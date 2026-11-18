"""Génération automatique des plans (api/plan_auto.py, 18/11/2026)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.plan_auto import generate, os_family  # noqa: E402


def vm(name, disks=1, gb=50, guest="ubuntu64Guest", power="poweredOn", ok=True):
    return {"moref": f"vm-{name}", "instance_uuid": f"u-{name}", "name": name,
            "power_state": power, "guest_id": guest, "num_disks": disks,
            "disk_capacity_gb": gb, "migratable": ok, "preflight_findings": [] if ok else ["CBT"]}


def inv(*vms, dc="DC1"):
    return {"datacenters": [{"name": dc, "vms": list(vms)}]}


def test_os_family():
    assert os_family("windows9Server64Guest") == "Windows"
    assert os_family("winNTGuest") == "Windows"
    assert os_family("other3xLinux64Guest") == "Linux"
    assert os_family("freebsd12_64Guest") == "Autre"


def test_disk_limit_and_exclusions():
    vms = [vm(f"w{i}", disks=5, guest="windows9_64Guest") for i in range(10)]
    vms += [vm("big", disks=30), vm("ko", ok=False), vm("seen")]
    out = generate(inv(*vms), {"u-seen"}, prefix="P", pilot_size=0)
    assert all(p["disks"] <= 24 for p in out["plans"])
    assert sum(len(p["vms"]) for p in out["plans"]) == 10
    reasons = {e["name"]: e["reason"] for e in out["excluded"]}
    assert set(reasons) == {"big", "ko", "seen"}
    assert "déjà dans un plan" == reasons["seen"]


def test_pilot_prefers_small_off_linux_and_windows_separate():
    vms = [vm("lin-big", gb=500), vm("off", gb=20, power="poweredOff"),
           vm("win", gb=10, guest="windows9_64Guest"), vm("lin-a", gb=30), vm("lin-b", gb=40)]
    out = generate(inv(*vms), set(), prefix="P")
    pilot = out["plans"][0]
    assert pilot["name"] == "P V00 – pilote"
    assert [v["name"] for v in pilot["vms"]] == ["off", "lin-a", "lin-b"]
    rest = {p["name"]: [v["name"] for v in p["vms"]] for p in out["plans"][1:]}
    assert rest == {"P V01 – Linux": ["lin-big"], "P V02 – Windows": ["win"]}


def test_capacity_limit_splits_and_oversized_alone():
    vms = [vm("a", gb=300), vm("b", gb=300), vm("huge", gb=2000)]
    out = generate(inv(*vms), set(), prefix="P", max_capacity_gb=500, pilot_size=0)
    assert [[v["name"] for v in p["vms"]] for p in out["plans"]] == [["a"], ["b"], ["huge"]]


def test_group_by_prefix_and_max_vms():
    vms = [vm("APP1-web"), vm("APP1-db"), vm("APP1-batch"), vm("CRM_srv"), vm("crm-db")]
    out = generate(inv(*vms), set(), prefix="P", pilot_size=0, group_by=("prefix",), max_vms=2)
    got = {p["name"]: sorted(v["name"] for v in p["vms"]) for p in out["plans"]}
    assert got == {"P V01 – APP1": ["APP1-batch", "APP1-db"], "P V02 – APP1": ["APP1-web"],
                   "P V03 – CRM": ["CRM_srv", "crm-db"]}


def test_analysis():
    vms = [vm("a", disks=1), vm("b", disks=30), vm("c", ok=False), vm("d", disks=3)]
    a = generate(inv(*vms), {"u-d"}, prefix="P")["analysis"]
    assert (a["vm_total"], a["vm_migratable"], a["vm_to_plan"], a["disks_to_plan"]) == (4, 3, 2, 31)
    assert a["over_max_disks"] == ["b"] and a["min_relay_rounds"] == 2
