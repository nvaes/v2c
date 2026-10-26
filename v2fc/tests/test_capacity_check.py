"""Vérification d'espace avant provisioning (api/routers/jobs.py, 26/10/2026)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routers.jobs import _capacity_problem  # noqa: E402

GIB = 1024 ** 3


class FakeClient:
    def __init__(self, free_gb, relay_disks):
        self.free_gb, self.relay_disks = free_gb, relay_disks

    def get_datastore(self, ds_id):
        return {"name": "LUN_TEST", "actualFreeSizeGB": self.free_gb}

    def get_vm(self, vm_id):
        return {"vmConfig": {"disks": [{}] * self.relay_disks}}


RELAY = SimpleNamespace(fc_vm_uri="/service/sites/S/vms/i-00000013")
PLAN_VM = SimpleNamespace(tgt_datastore_urn="5", tgt_datastore_name="LUN_TEST")


def test_enough_space_and_slots():
    assert _capacity_problem(FakeClient(500, 2), RELAY, PLAN_VM,
                             [{"capacity_bytes": 100 * GIB}]) is None


def test_refuses_when_datastore_too_small():
    # 100 Gio source -> volume de 101 Go (secteur de signature, arrondi)
    msg = _capacity_problem(FakeClient(100, 2), RELAY, PLAN_VM, [{"capacity_bytes": 100 * GIB}])
    assert "espace insuffisant" in msg and "101 Go" in msg


def test_refuses_when_relay_has_no_free_virtio_slot():
    msg = _capacity_problem(FakeClient(10_000, 24), RELAY, PLAN_VM,
                            [{"capacity_bytes": GIB}, {"capacity_bytes": GIB}])
    assert "24 disques sur 25" in msg
