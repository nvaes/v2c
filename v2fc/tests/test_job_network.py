"""Ligne « Réseau » de la page Migration (api/routers/jobs.py, 03/11/2026)."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routers.jobs import (_merge_source_network, _source_network,  # noqa: E402
                              _target_network)


def test_cible_dans_lordre_de_creation_sans_ip_factice():
    net = _target_network([
        {"name": "Network Adapter 1", "sequenceNum": 1, "mac": "28:6e:d4:89:bc:dc",
         "portGroupName": "DMZ", "ip": "0.0.0.0", "ipList": "0.0.0.0", "ips6": []},
        {"name": "Network Adapter 0", "sequenceNum": 0, "mac": "28:6e:d4:89:bc:db",
         "portGroupName": "LAN", "ip": "10.1.2.3", "ipList": "10.1.2.3,10.1.2.4",
         "ips6": ["fe80::1", "2001:db8::5"]},
    ])
    assert [n["network"] for n in net["nics"]] == ["LAN", "DMZ"]
    assert net["nics"][0]["ips"] == ["10.1.2.3", "10.1.2.4", "2001:db8::5"]
    assert net["nics"][1]["ips"] == []


def test_source_ip_par_carte_sans_lien_local():
    raw = SimpleNamespace(guest=SimpleNamespace(net=[
        SimpleNamespace(deviceConfigId=4000, ipAddress=["192.168.1.10", "fe80::abcd"]),
        SimpleNamespace(deviceConfigId=-1, ipAddress=["127.0.0.1"]),
    ]))
    vm = SimpleNamespace(raw=raw, nics=lambda: [
        {"device_key": 4000, "label": "Network adapter 1", "mac": "a", "network": "VM Network"},
        {"device_key": 4001, "label": "Network adapter 2", "mac": "b", "network": "DMZ"},
    ])
    nics = _source_network(vm)
    assert nics[0]["ips"] == ["192.168.1.10"] and nics[1]["ips"] == []


def test_vm_eteinte_garde_les_ip_du_releve_precedent():
    before = {"nics": [{"device_key": 4000, "ips": ["192.168.1.10"]}]}
    merged = _merge_source_network(before, [{"device_key": 4000, "label": "x", "mac": "a",
                                             "network": "VM Network", "ips": []}])
    assert merged["nics"][0]["ips"] == ["192.168.1.10"]
