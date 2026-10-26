"""Session vCenter maintenue pendant les longues copies VDDK."""
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("pyVmomi")
from datamover.vsphere import VSphereClient  # noqa: E402


class _Sessions:
    def __init__(self, alive):
        self.currentSession = object() if alive else None
        self.logins = []

    def Login(self, userName, password):
        self.logins.append((userName, password))
        self.currentSession = object()


def _client(alive, idle):
    c = VSphereClient.__new__(VSphereClient)
    c.host, c.user, c._password = "vc", "svc", "secret"
    c.content = SimpleNamespace(sessionManager=_Sessions(alive))
    c._last_seen = time.monotonic() - idle
    return c


def test_session_expiree_reconnectee():
    c = _client(alive=False, idle=600)
    c.keepalive()
    assert c.content.sessionManager.logins == [("svc", "secret")]


def test_session_vivante_pas_de_login():
    c = _client(alive=True, idle=600)
    c.keepalive()
    assert c.content.sessionManager.logins == []


def test_appel_recent_rien_a_faire():
    c = _client(alive=False, idle=10)
    c.keepalive()
    assert c.content.sessionManager.logins == []
