"""
Planificateur d'extents — transforme ce que renvoie vSphere en plan de copie.

Entrées possibles :
  * les zones modifiées d'un cycle CBT (DiskChangeInfo.changedArea, issu de
    QueryChangedDiskAreas) ;
  * les blocs alloués d'un seed (VixDiskLib_QueryAllocatedBlocks).

Sortie : une suite de tranches (Chunk) numérotées, matérialisée en base
AVANT le début du transfert. C'est cette numérotation qui rend la reprise
après pause exacte (ARCHITECTURE.md §8.3) : le curseur persisté désigne un
indice de tranche, pas une position approximative.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

from .vddk import SECTOR_SIZE, Extent, merge_extents

DEFAULT_CHUNK = 4 << 20          # 4 Mio — compromis débit / granularité de reprise


@dataclass(frozen=True)
class Chunk:
    """Une unité de transfert. `idx` est stable pour un plan donné."""
    idx: int
    offset: int
    length: int

    @property
    def end(self) -> int:
        return self.offset + self.length


class PlanError(ValueError):
    pass


def align_extents(extents: Iterable[Extent], capacity: int,
                  sector: int = SECTOR_SIZE) -> list[Extent]:
    """
    Aligne les extents sur le secteur en ÉLARGISSANT vers l'extérieur, puis
    fusionne et borne à la capacité.

    Élargir plutôt que rétrécir n'est pas un détail : rétrécir laisserait des
    octets modifiés non copiés, ce qui produirait une cible corrompue sans
    aucun symptôme. Copier quelques secteurs en trop ne coûte rien.
    """
    out: list[Extent] = []
    for e in extents:
        if e.length <= 0:
            continue
        if e.offset < 0:
            raise PlanError(f"offset négatif : {e.offset}")
        start = (e.offset // sector) * sector
        end = -(-e.end // sector) * sector        # arrondi supérieur
        end = min(end, capacity)
        if end > start:
            out.append(Extent(start, end - start))
    return merge_extents(out)


def build_plan(extents: Sequence[Extent], capacity: int,
               chunk_bytes: int = DEFAULT_CHUNK,
               sector: int = SECTOR_SIZE) -> list[Chunk]:
    """
    Construit le plan de copie complet : alignement, fusion, découpage.

    Le découpage ne franchit jamais une frontière d'extent — une tranche
    porte toujours des octets réellement à copier.
    """
    if chunk_bytes % sector:
        raise PlanError(f"chunk_bytes doit être multiple de {sector}")
    if capacity % sector:
        raise PlanError(f"capacité non alignée : {capacity}")

    plan: list[Chunk] = []
    idx = 0
    for e in align_extents(extents, capacity, sector):
        pos = e.offset
        while pos < e.end:
            n = min(chunk_bytes, e.end - pos)
            plan.append(Chunk(idx, pos, n))
            idx += 1
            pos += n
    return plan


def resume_from(plan: Sequence[Chunk], cursor_index: int) -> list[Chunk]:
    """
    Reprend un plan à partir du curseur persisté.

    `cursor_index` est l'indice de la PREMIÈRE tranche non confirmée.
    La tranche est donc rejouée si le transfert s'est interrompu en plein
    milieu : réécrire une tranche est idempotent, en sauter une ne l'est pas.
    """
    if cursor_index < 0:
        raise PlanError("curseur négatif")
    return [c for c in plan if c.idx >= cursor_index]


def total_bytes(plan: Iterable[Chunk]) -> int:
    return sum(c.length for c in plan)


# --------------------------------------------------------------------------
# Adaptation depuis vSphere
# --------------------------------------------------------------------------
def extents_from_changed_areas(changed_areas) -> list[Extent]:
    """
    Convertit un `vim.VirtualMachine.DiskChangeInfo.changedArea` en extents.

    Accepte les objets pyVmomi (attributs .start / .length) comme les
    dictionnaires, pour pouvoir tester le planificateur sans vCenter.
    """
    out: list[Extent] = []
    for area in changed_areas:
        if isinstance(area, dict):
            start, length = area["start"], area["length"]
        else:
            start, length = area.start, area.length
        out.append(Extent(int(start), int(length)))
    return out


def query_changed_areas_complete(query_fn, capacity: int, change_id: str,
                                 max_iterations: int = 10_000):
    """
    Boucle d'appel de QueryChangedDiskAreas jusqu'à couvrir tout le disque.

    vSphere ne garantit pas de répondre en un seul appel : la réponse porte
    un `startOffset` et une `length`, et il faut relancer depuis la fin de la
    plage couverte tant qu'on n'a pas atteint la capacité. Oublier cette
    boucle fait perdre silencieusement la fin du disque.

    `query_fn(start_offset) -> objet avec .startOffset, .length, .changedArea`
    """
    extents: list[Extent] = []
    covered = 0
    for _ in range(max_iterations):
        if covered >= capacity:
            break
        result = query_fn(covered)
        extents.extend(extents_from_changed_areas(result.changedArea or []))
        advance = int(result.startOffset) + int(result.length)
        if advance <= covered:
            raise PlanError(
                f"QueryChangedDiskAreas n'avance pas (covered={covered}, "
                f"retour={advance}) — changeId {change_id!r} probablement invalide")
        covered = advance
    else:
        raise PlanError("QueryChangedDiskAreas : trop d'itérations")

    if covered < capacity:
        raise PlanError(f"couverture incomplète : {covered} < {capacity}")
    return merge_extents(extents)


def is_zero(data: bytes) -> bool:
    """
    Détection de zéros. `bytes.count(0)` est implémenté en C et va bien plus
    vite qu'une comparaison à un tampon nul alloué à chaque appel.
    """
    return data.count(0) == len(data)
