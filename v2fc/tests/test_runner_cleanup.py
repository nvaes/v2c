"""Nettoyage des dossiers d'état du runner (tools/v2fc_runner.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import v2fc_runner

KEPT = "11111111-1111-1111-1111-111111111111"
RUNNING = "22222222-2222-2222-2222-222222222222"
GONE = "33333333-3333-3333-3333-333333333333"


class Resp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


def make_state(root: Path) -> None:
    for name in (KEPT, RUNNING, GONE, "lab-test"):
        (root / name).mkdir()
        (root / name / "state.json").write_text("{}")


def test_supprime_seulement_les_jobs_disparus(tmp_path, monkeypatch):
    make_state(tmp_path)
    monkeypatch.setattr(v2fc_runner, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(v2fc_runner, "_get", lambda path: Resp([KEPT]))
    v2fc_runner._cleanup_state({RUNNING: ("seed", None)})
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([KEPT, RUNNING, "lab-test"])


def test_api_injoignable_rien_supprime(tmp_path, monkeypatch):
    make_state(tmp_path)
    monkeypatch.setattr(v2fc_runner, "STATE_ROOT", tmp_path)

    def fail(path):
        raise requests.ConnectionError("api down")
    monkeypatch.setattr(v2fc_runner, "_get", fail)
    v2fc_runner._cleanup_state({})
    assert len(list(tmp_path.iterdir())) == 4
