"""Détection d'un invité Windows pour le popup des pilotes (api/vsphere_bridge.py, 28/10/2026)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.vsphere_bridge import is_windows_guest  # noqa: E402


def test_windows_by_guest_id_including_old_versions():
    assert is_windows_guest("windows9Server64Guest", "")
    assert is_windows_guest("winNetStandardGuest", "")      # 2003
    assert is_windows_guest("winLonghorn64Guest", "")       # 2008


def test_windows_by_name_when_guest_id_is_generic():
    assert is_windows_guest("otherGuest64", "Microsoft Windows Server 2019 (64-bit)")


def test_linux_is_not_windows():
    assert not is_windows_guest("centos7_64Guest", "CentOS 7 (64-bit)")
    assert not is_windows_guest("", "")
