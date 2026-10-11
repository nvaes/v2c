"""
Pré-vérifications d'une VM source avant migration.

Le but n'est pas de produire un rapport, c'est d'empêcher les migrations qui
échoueront — et surtout celles qui réussiront en apparence avec une cible
incomplète. Un disque indépendant n'est pas suivi par le CBT et n'entre pas
dans les snapshots : le copier « au mieux » produirait une cible corrompue
sans le moindre message d'erreur. On refuse.

Chaque constat porte une sévérité :
  BLOCK — le job ne peut pas démarrer ;
  WARN  — le job peut démarrer, mais l'opérateur doit savoir ;
  INFO  — information de dimensionnement ou de préparation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .vsphere import DiskSpec, SourceVm


class Severity(str, Enum):
    BLOCK = "BLOCK"
    WARN = "WARN"
    INFO = "INFO"


@dataclass
class Finding:
    severity: Severity
    code: str
    message: str
    remedy: str = ""
    subject: str = ""

    def __str__(self) -> str:
        head = f"[{self.severity.value:5}] {self.code}"
        sub = f" ({self.subject})" if self.subject else ""
        return f"{head}{sub} : {self.message}"


@dataclass
class PreflightReport:
    vm_name: str
    findings: list[Finding] = field(default_factory=list)

    def add(self, severity: Severity, code: str, message: str,
            remedy: str = "", subject: str = "") -> None:
        self.findings.append(Finding(severity, code, message, remedy, subject))

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCK]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]

    @property
    def ok(self) -> bool:
        return not self.blockers

    def render(self) -> str:
        lines = [f"Pré-vérification : {self.vm_name}"]
        for sev in (Severity.BLOCK, Severity.WARN, Severity.INFO):
            for f in (x for x in self.findings if x.severity is sev):
                lines.append(f"  {f}")
                if f.remedy and sev is not Severity.INFO:
                    lines.append(f"          → {f.remedy}")
        verdict = "MIGRATION POSSIBLE" if self.ok else \
                  f"MIGRATION REFUSÉE ({len(self.blockers)} blocage(s))"
        lines.append(f"  {verdict}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Contrôles
# --------------------------------------------------------------------------
def _check_vm_level(vm: SourceVm, rep: PreflightReport) -> None:
    if vm.raw.config.template:
        rep.add(Severity.BLOCK, "TEMPLATE",
                "objet modèle (template), pas une VM",
                "convertir en VM ou choisir une autre source")

    if vm.is_encrypted:
        rep.add(Severity.BLOCK, "VM_CHIFFREE",
                "VM chiffrée : le VDDK ne peut pas lire ses disques",
                "déchiffrer la VM avant migration, ou migrer par une autre voie")

    owner = vm.managed_by
    agent_signal = vm.agent_signal
    if owner or agent_signal:
        why = f"gérée par « {owner} »" if owner else agent_signal
        rep.add(Severity.BLOCK, "VM_AGENT",
                f"VM d'agent ({why}) — vCLS, NSX, vSAN, EAM…",
                "ne pas migrer : ces VM sont déployées et recréées "
                "automatiquement par vCenter sur la plateforme de destination")

    if vm.host_connection:
        rep.add(Severity.WARN, "CONNEXION_HOTE",
                "session ouverte directement sur l'ESXi, pas sur un vCenter",
                "PrepareForAccess ne peut alors pas suspendre DRS ni vMotion : "
                "si cet hôte appartient à un cluster, un déplacement décidé "
                "par vCenter en cours de cycle invaliderait la lecture. "
                "Acceptable en laboratoire, à proscrire en production")

    if vm.firmware not in ("bios", "efi"):
        rep.add(Severity.WARN, "FIRMWARE_INCONNU",
                f"firmware « {vm.firmware} » non reconnu")
    else:
        rep.add(Severity.INFO, "FIRMWARE",
                f"firmware {vm.firmware.upper()} — la VM cible doit être créée "
                f"à l'identique, sinon le disque n'est pas amorçable")

    if not vm.cbt_enabled:
        rep.add(Severity.WARN, "CBT_INACTIF",
                "CBT désactivé : sans lui, chaque cycle recopierait tout le disque",
                "l'outil l'activera, ce qui impose un cycle de stun "
                "(création puis suppression immédiate d'un snapshot)")

    existing = [s.name for s in vm.snapshots]
    foreign = [n for n in existing if not n.startswith("v2fc")]
    if foreign:
        rep.add(Severity.WARN, "SNAPSHOTS_EXISTANTS",
                f"{len(foreign)} snapshot(s) préexistant(s) : {', '.join(foreign[:3])}",
                "les consolider avant migration : ils allongent la chaîne de "
                "redo et ralentissent chaque lecture")

    if vm.tools_status not in ("toolsOk", "toolsOld"):
        rep.add(Severity.WARN, "TOOLS_ABSENT",
                f"VMware Tools : {vm.tools_status}",
                "sans Tools, pas d'arrêt propre de l'invité à la bascule : "
                "l'extinction sera forcée, avec risque pour les systèmes de "
                "fichiers et les bases de données")

    if vm.guest_id:
        rep.add(Severity.INFO, "INVITE", f"guestId = {vm.guest_id}")


def _check_disk(disk: DiskSpec, rep: PreflightReport) -> int:
    """Renvoie la capacité du disque si migrable, 0 sinon."""
    sub = f"{disk.label}, {disk.capacity_bytes / 2**30:.1f} Gio"

    if disk.is_independent:
        rep.add(Severity.BLOCK, "DISQUE_INDEPENDANT",
                f"mode « {disk.disk_mode} » : exclu des snapshots, non suivi "
                f"par le CBT",
                "passer le disque en mode dépendant, ou l'exclure "
                "explicitement du périmètre et le traiter à part",
                subject=sub)
        return 0

    if disk.is_physical_rdm:
        rep.add(Severity.BLOCK, "RDM_PHYSIQUE",
                "RDM en compatibilité physique : ni snapshot ni CBT",
                "convertir en RDM virtuel ou en VMDK, ou migrer ce LUN par "
                "réplication de baie",
                subject=sub)
        return 0

    if disk.is_rdm:
        rep.add(Severity.WARN, "RDM_VIRTUEL",
                "RDM en compatibilité virtuelle : migrable, mais la cible sera "
                "un volume ordinaire, pas un RDM",
                "vérifier qu'aucun cluster invité ne dépend du LUN partagé",
                subject=sub)

    if disk.is_multi_writer:
        rep.add(Severity.BLOCK, "MULTI_WRITER",
                "disque partagé multi-writer : appartient probablement à un "
                "cluster invité",
                "migrer tous les nœuds du cluster ensemble, avec un plan dédié",
                subject=sub)
        return 0

    if disk.capacity_bytes % 512:
        rep.add(Severity.BLOCK, "CAPACITE_NON_ALIGNEE",
                f"capacité {disk.capacity_bytes} non multiple de 512",
                subject=sub)
        return 0

    if disk.capacity_bytes == 0:
        rep.add(Severity.BLOCK, "CAPACITE_NULLE", "capacité nulle", subject=sub)
        return 0

    return disk.capacity_bytes


def run_preflight(vm: SourceVm, snapshot=None) -> PreflightReport:
    rep = PreflightReport(vm_name=vm.name)
    _check_vm_level(vm, rep)

    disks = vm.disks(snapshot)
    if not disks:
        rep.add(Severity.BLOCK, "AUCUN_DISQUE", "la VM ne porte aucun disque")
        return rep

    total = sum(_check_disk(d, rep) for d in disks)
    rep.add(Severity.INFO, "VOLUMETRIE",
            f"{len(disks)} disque(s), {total / 2**30:.1f} Gio à provisionner "
            f"côté cible (hors réservoir de signature)")

    controllers = {d.controller_key for d in disks}
    if len(controllers) > 1:
        rep.add(Severity.INFO, "CONTROLEURS",
                f"{len(controllers)} contrôleurs : l'ordre contrôleur/unité doit "
                f"être reproduit à l'identique côté cible")

    if vm.power_state == "poweredOn":
        rep.add(Severity.INFO, "ETAT",
                "VM allumée : le seed et les deltas se feront à chaud, seul le "
                "dernier delta impose l'extinction")
    else:
        rep.add(Severity.INFO, "ETAT",
                "VM éteinte : la migration peut se faire en une passe à froid")

    return rep
