"""Cartes réseau de la VM cible à la bascule (api/routers/jobs.py, 26/10/2026)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routers.jobs import _target_nics  # noqa: E402


def _nic(key, urn, mac="", keep_mac=False):
    return {"device_key": key, "tgt_network_urn": urn, "mac": mac, "keep_mac": keep_mac}


def test_one_card_per_mapped_nic_in_source_order_with_optional_mac():
    plan_vm = SimpleNamespace(tgt_network_urn="A", nic_mappings=[
        _nic(4001, "B", "00:50:56:a8:15:58", keep_mac=True),
        _nic(4000, "A", "00:50:56:a8:37:de"),
        _nic(4002, None, "00:50:56:a8:00:01", keep_mac=True),   # non recréée
    ])
    assert _target_nics(plan_vm) == [
        {"portGroupUrn": "A"},
        {"portGroupUrn": "B", "mac": "00:50:56:a8:15:58"},
    ]


def test_plan_without_nic_mappings_keeps_single_network():
    plan_vm = SimpleNamespace(tgt_network_urn="OLD", nic_mappings=[])
    assert _target_nics(plan_vm) == [{"portGroupUrn": "OLD"}]
