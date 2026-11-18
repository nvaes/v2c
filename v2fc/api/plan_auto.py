"""
Génération automatique des plans pour une migration complète (18/11/2026).

Rien n'est propre à un client : le découpage se calcule sur l'inventaire de
la source choisie, avec des critères réglables à l'écran. Pratiques usuelles
de migration par lots appliquées :

- seules les VM migrables (pré-vérification OK) et pas déjà dans un plan ;
- au plus `max_disks` disques par plan : tout le plan doit pouvoir tenir en
  même temps sur le relais (25 emplacements virtio sur FusionCompute, moins
  les disques propres du relais — l'interface propose la valeur lue sur le
  relais réel) ; une VM qui dépasse seule est écartée ;
- au plus `max_capacity_gb` (et `max_vms` si non nul) par plan, pour garder
  des seeds et des fenêtres de bascule de durée raisonnable ; une VM plus
  grosse que la limite de volume forme un plan à elle seule ;
- une vague pilote d'abord (`pilot_size` VM, 0 = aucune) : petites VM,
  éteintes et Linux de préférence, pour valider la chaîne de bout en bout ;
- puis des vagues regroupées selon `group_by` (datacenter, famille d'OS,
  état d'alimentation, préfixe de nom — souvent l'application ou le site
  dans les conventions de nommage), Linux avant Windows (pilotes virtio à
  installer avant la bascule côté Windows), petites VM d'abord.

`analyse()` décrit l'inventaire (répartitions, plus grosses VM, préfixes
détectés, nombre minimal de remplissages du relais) pour choisir les critères.

Fonctions pures (pas d'accès base ni réseau) : tests/test_plan_auto.py.
"""
from __future__ import annotations

import math
import re
from collections import Counter

GROUP_KEYS = ("dc", "os", "power", "prefix")
PILOT_MAX_GB = 100
_OS_ORDER = {"Linux": 0, "Autre": 1, "Windows": 2}

# guestId VMware (VirtualMachineGuestOsIdentifier) : familles Linux connues.
_LINUX_HINTS = ("linux", "ubuntu", "debian", "rhel", "centos", "sles", "suse", "oracle",
                "rocky", "alma", "fedora", "photon", "coreos", "amazon", "mandr", "turbo",
                "asianux", "redhat", "kylin", "openeuler", "euleros")


def os_family(guest_id: str) -> str:
    g = (guest_id or "").lower()
    if g.startswith("win"):
        return "Windows"
    if any(h in g for h in _LINUX_HINTS):
        return "Linux"
    return "Autre"


def name_prefix(name: str) -> str:
    """Premier segment du nom (avant -, _, . ou espace), en majuscules."""
    head = re.split(r"[-_.\s]", name.strip(), maxsplit=1)[0]
    return (head or name).upper()


def _rows(inventory: dict) -> list[dict]:
    return [{**vm, "dc": dc["name"], "os": os_family(vm.get("guest_id", "")),
             "prefix": name_prefix(vm["name"]),
             "power": "éteinte" if vm.get("power_state") == "poweredOff" else "allumée"}
            for dc in inventory.get("datacenters", []) for vm in dc.get("vms", [])]


def analyse(inventory: dict, already_planned: set[str], *, max_disks: int) -> dict:
    rows = _rows(inventory)
    todo = [v for v in rows if v.get("migratable") and v["instance_uuid"] not in already_planned]
    disks = sum(v["num_disks"] for v in todo)
    buckets = Counter("1" if v["num_disks"] == 1 else "2-3" if v["num_disks"] <= 3
                      else "4-8" if v["num_disks"] <= 8 else "9+" for v in todo)
    prefixes = Counter(v["prefix"] for v in todo)
    return {
        "vm_total": len(rows),
        "vm_migratable": sum(1 for v in rows if v.get("migratable")),
        "vm_already_planned": sum(1 for v in rows if v["instance_uuid"] in already_planned),
        "vm_to_plan": len(todo),
        "disks_to_plan": disks,
        "capacity_gb_to_plan": round(sum(v["disk_capacity_gb"] for v in todo), 1),
        "by_os": dict(Counter(v["os"] for v in todo)),
        "by_dc": dict(Counter(v["dc"] for v in todo)),
        "by_power": dict(Counter(v["power"] for v in todo)),
        "by_disks": {k: buckets.get(k, 0) for k in ("1", "2-3", "4-8", "9+")},
        # Préfixes partagés par au moins 2 VM : utiles seulement s'il y en a.
        "prefixes": dict(c for c in prefixes.most_common(15) if c[1] > 1),
        "largest": [{"name": v["name"], "num_disks": v["num_disks"],
                     "disk_capacity_gb": v["disk_capacity_gb"]}
                    for v in sorted(todo, key=lambda v: -v["disk_capacity_gb"])[:5]],
        "over_max_disks": [v["name"] for v in todo if v["num_disks"] > max_disks],
        # Remplissage parfait impossible (une VM ne se coupe pas) : plancher.
        "min_relay_rounds": math.ceil(disks / max_disks) if max_disks else 0,
    }


def generate(inventory: dict, already_planned: set[str], *, prefix: str,
             max_disks: int = 24, max_capacity_gb: float = 4096, max_vms: int = 0,
             pilot_size: int = 3, group_by: tuple[str, ...] = ("dc", "os")) -> dict:
    group_by = tuple(k for k in GROUP_KEYS if k in group_by)
    plans: list[dict] = []
    excluded: list[dict] = []
    candidates: list[dict] = []

    for v in _rows(inventory):
        if v["instance_uuid"] in already_planned:
            excluded.append({"name": v["name"], "reason": "déjà dans un plan"})
        elif not v.get("migratable"):
            findings = v.get("preflight_findings") or []
            blocks = [f.split(":", 1)[-1].strip() for f in findings if "BLOCK" in f]
            reason = "; ".join(blocks or findings) or "non migrable"
            excluded.append({"name": v["name"], "reason": f"pré-vérification : {reason}"})
        elif v["num_disks"] > max_disks:
            excluded.append({"name": v["name"],
                             "reason": f"{v['num_disks']} disques, plus que les {max_disks} "
                                       "d'un plan : à migrer seule, relais vidé"})
        else:
            candidates.append({**v, "alone": v["disk_capacity_gb"] > max_capacity_gb})

    def size_key(v: dict) -> tuple:
        return (v["disk_capacity_gb"], v["num_disks"], v["name"].lower())

    if pilot_size > 0 and len(candidates) > pilot_size:
        pool = [v for v in candidates if not v["alone"] and v["disk_capacity_gb"] <= PILOT_MAX_GB]
        pool.sort(key=lambda v: (v["power"] != "éteinte", v["os"] != "Linux", size_key(v)))
        chosen, disks = [], 0
        for v in pool:
            if len(chosen) == pilot_size:
                break
            if disks + v["num_disks"] <= max_disks:
                chosen.append(v)
                disks += v["num_disks"]
        if chosen:
            ids = {v["instance_uuid"] for v in chosen}
            candidates = [v for v in candidates if v["instance_uuid"] not in ids]
            plans.append(_plan(f"{prefix} V00 – pilote", chosen))

    groups: dict[tuple, list[dict]] = {}
    for v in candidates:
        groups.setdefault(tuple(v[k] for k in group_by), []).append(v)
    # Une valeur commune à toutes les VM n'apporte rien dans le nom du plan.
    shown = [i for i in range(len(group_by)) if len({g[i] for g in groups}) > 1]

    def group_order(key: tuple) -> tuple:
        os_rank = _OS_ORDER.get(key[group_by.index("os")], 1) if "os" in group_by else 0
        return (os_rank, key)

    wave = 1
    for key in sorted(groups, key=group_order):
        vms = sorted(groups[key], key=size_key)
        batches: list[list[dict]] = []
        current: list[dict] = []
        disks = cap = 0.0
        for v in (v for v in vms if not v["alone"]):
            if current and (disks + v["num_disks"] > max_disks
                            or cap + v["disk_capacity_gb"] > max_capacity_gb
                            or (max_vms and len(current) >= max_vms)):
                batches.append(current)
                current, disks, cap = [], 0, 0.0
            current.append(v)
            disks += v["num_disks"]
            cap += v["disk_capacity_gb"]
        if current:
            batches.append(current)
        batches += [[v] for v in vms if v["alone"]]
        label = "".join(f" – {key[i]}" for i in shown)
        for batch in batches:
            plans.append(_plan(f"{prefix} V{wave:02d}{label}", batch))
            wave += 1

    return {"plans": plans, "excluded": excluded,
            "analysis": analyse(inventory, already_planned, max_disks=max_disks),
            "max_disks": max_disks, "max_capacity_gb": max_capacity_gb}


def _plan(name: str, vms: list[dict]) -> dict:
    return {
        "name": name,
        "disks": sum(v["num_disks"] for v in vms),
        "capacity_gb": round(sum(v["disk_capacity_gb"] for v in vms), 1),
        "vms": [{"moref": v["moref"], "instance_uuid": v["instance_uuid"], "name": v["name"],
                 "num_disks": v["num_disks"], "disk_capacity_gb": v["disk_capacity_gb"],
                 "os": v["os"], "power_state": v["power_state"]} for v in vms],
    }
