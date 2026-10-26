"""
Tests du driver cible FusionCompute, sans VRM réel.

Pas de bibliothèque de mock HTTP disponible dans l'environnement (ni
`responses` ni `requests_mock`) : on remplace directement `requests.Session`
par une fausse implémentation qui rejoue des réponses préparées à l'avance,
indexées par (méthode, chemin). C'est volontairement bas niveau — le but est
de vérifier que le driver construit exactement les requêtes documentées en
ARCHITECTURE.md §7.5 (en-têtes d'authentification, chemins, clés de
réponse), pas de tester `requests` lui-même.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datamover import fusioncompute as fc


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        self.text = text
        self.content = b"1" if json_data is not None else b""

    def json(self):
        if self._json is None:
            raise ValueError("pas de corps JSON")
        return self._json


class FakeSession:
    """
    Enregistre chaque appel (méthode, url, headers, params) et renvoie la
    réponse préparée pour ce chemin — les tests inspectent `.calls` pour
    vérifier ce qui a été effectivement envoyé.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.responses: dict[tuple[str, str], FakeResponse] = {}

    def on(self, method: str, path_suffix: str, response: FakeResponse) -> None:
        self.responses[(method, path_suffix)] = response

    def _dispatch(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        for (m, suffix), resp in self.responses.items():
            if m == method and url.endswith(suffix):
                return resp
        raise AssertionError(f"aucune réponse simulée pour {method} {url}")

    def get(self, url, headers=None, params=None, verify=None, timeout=None):
        return self._dispatch("GET", url, headers=headers, params=params)

    def post(self, url, headers=None, json=None, verify=None, timeout=None):
        return self._dispatch("POST", url, headers=headers, json=json)

    def delete(self, url, headers=None, verify=None, timeout=None):
        return self._dispatch("DELETE", url, headers=headers)


SITE_RESPONSE = FakeResponse(json_data={
    "sites": [{"urn": "urn:sites:05C9108A", "uri": "/service/sites/05C9108A",
              "name": "site-lab"}]
})


def make_client(monkeypatch, session: FakeSession, **kwargs) -> fc.FusionComputeClient:
    monkeypatch.setattr(fc.requests, "Session", lambda: session)
    session.on("POST", "/service/session",
              FakeResponse(headers={"X-Auth-Token": "TOKEN123"}, json_data={"validity": 600000}))
    session.on("GET", "/service/sites", SITE_RESPONSE)
    return fc.FusionComputeClient("172.25.99.230", "admin", "secret", port=7443,
                                  verify=False, **kwargs)


# --------------------------------------------------------------------------
# Connexion (§7.5 ①)
# --------------------------------------------------------------------------
def test_login_sends_documented_headers_and_hashed_password(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("DELETE", "/service/session", FakeResponse())

    login_call = next(c for c in session.calls if c["url"].endswith("/session")
                      and c["method"] == "POST")
    headers = login_call["headers"]
    assert headers["X-Auth-User"] == "admin"
    assert headers["X-Auth-Key"] == hashlib.sha256(b"secret").hexdigest()
    assert headers["X-Auth-UserType"] == "2"       # défaut documenté : interconnexion
    assert headers["X-Auth-AuthType"] == "0"       # défaut documenté : locale
    assert client._token == "TOKEN123"
    assert client.site.id == "05C9108A"
    assert client.site.name == "site-lab"
    client.close()


def test_login_failure_raises_with_server_detail(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(fc.requests, "Session", lambda: session)
    session.on("POST", "/service/session",
              FakeResponse(status_code=400, json_data={"errorCode": "10100103"}))
    with pytest.raises(fc.FusionComputeError, match="10100103"):
        fc.FusionComputeClient("172.25.99.230", "admin", "wrong", verify=False)


def test_missing_token_header_is_an_error(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(fc.requests, "Session", lambda: session)
    session.on("POST", "/service/session", FakeResponse(json_data={}))  # pas de X-Auth-Token
    with pytest.raises(fc.FusionComputeError, match="X-Auth-Token"):
        fc.FusionComputeClient("172.25.99.230", "admin", "secret", verify=False)


def test_close_calls_delete_session_and_swallows_errors(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("DELETE", "/service/session", FakeResponse())
    client.close()
    assert any(c["method"] == "DELETE" and c["url"].endswith("/session")
              for c in session.calls)
    client.close()  # idempotent, ne doit pas relever d'appel ni lever


# --------------------------------------------------------------------------
# Datastores (agrégation par hôte, dédupliquée par urn)
# --------------------------------------------------------------------------
def test_list_datastores_dedupes_shared_datastore_across_hosts(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)

    session.on("GET", "/sites/05C9108A/hosts", FakeResponse(json_data={"hosts": [
        {"urn": "urn:sites:05C9108A:hosts:1", "name": "esxi-1"},
        {"urn": "urn:sites:05C9108A:hosts:2", "name": "esxi-2"},
    ]}))
    shared_ds = {"urn": "urn:sites:05C9108A:datastores:9", "name": "ds-partagé",
                "actualFreeSizeGB": 500}
    session.on("GET", "/sites/05C9108A/hosts/1/datastores",
              FakeResponse(json_data={"datastores": [shared_ds]}))
    session.on("GET", "/sites/05C9108A/hosts/2/datastores",
              FakeResponse(json_data={"datastores": [shared_ds]}))

    out = client.list_datastores()
    assert len(out) == 1
    assert out[0]["urn"] == shared_ds["urn"]


def test_list_datastores_completes_bare_urn_from_host_listing(monkeypatch):
    # VRM réel (26/10/2026) : la liste par hôte renvoie "urn": "5".
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("GET", "/sites/05C9108A/hosts", FakeResponse(json_data={"hosts": [
        {"urn": "urn:sites:05C9108A:hosts:1", "name": "cna-1"},
    ]}))
    session.on("GET", "/sites/05C9108A/hosts/1/datastores",
              FakeResponse(json_data={"datastores": [{"urn": "5", "name": "LUN_5"}]}))

    out = client.list_datastores()
    assert out[0]["urn"] == "urn:sites:05C9108A:datastores:5"


# --------------------------------------------------------------------------
# Réseaux (DVS -> port groups, annotés du DVS parent)
# --------------------------------------------------------------------------
def test_list_networks_flattens_portgroups_with_dvswitch_annotation(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)

    session.on("GET", "/sites/05C9108A/dvswitchs", FakeResponse(json_data={"dvSwitchs": [
        {"urn": "urn:sites:05C9108A:dvswitchs:1", "name": "dvs-prod"},
    ]}))
    session.on("GET", "/sites/05C9108A/dvswitchs/1/portgroups", FakeResponse(json_data={
        "portgroups": [
            {"urn": "urn:sites:05C9108A:portgroups:1", "name": "VLAN100", "vlanId": 100},
        ]
    }))

    out = client.list_networks()
    assert len(out) == 1
    assert out[0]["dvswitch_name"] == "dvs-prod"
    assert out[0]["dvswitch_urn"] == "urn:sites:05C9108A:dvswitchs:1"
    assert out[0]["vlanId"] == 100


def test_list_portgroups_accepts_both_documented_key_spellings(monkeypatch):
    """
    Incohérence documentaire relevée dans la source (§7.5, note de module) :
    la description de l'API dit `portGroups`, l'exemple de réponse dit
    `portgroups`. Le driver doit tolérer les deux.
    """
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("GET", "/sites/05C9108A/dvswitchs/1/portgroups", FakeResponse(json_data={
        "portGroups": [{"urn": "urn:x:1", "name": "seul-camelcase"}]
    }))
    out = client.list_portgroups("1")
    assert out == [{"urn": "urn:x:1", "name": "seul-camelcase"}]


# --------------------------------------------------------------------------
# Tâches asynchrones (§7.5 « Suivi de tâche »)
# --------------------------------------------------------------------------
def test_wait_task_polls_until_success(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)

    calls = {"n": 0}
    real_dispatch = session._dispatch

    def flaky_dispatch(method, url, **kwargs):
        if url.endswith("/tasks/42"):
            calls["n"] += 1
            if calls["n"] < 3:
                return FakeResponse(json_data={"status": "running", "progress": 10 * calls["n"]})
            return FakeResponse(json_data={"status": "success"})
        return real_dispatch(method, url, **kwargs)

    session._dispatch = flaky_dispatch
    result = client.wait_task("/service/sites/05C9108A/tasks/42", poll_interval=0)
    assert result["status"] == "success"
    assert calls["n"] == 3


def test_wait_task_raises_on_failure(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("GET", "/sites/05C9108A/tasks/42",
              FakeResponse(json_data={"status": "failed", "reasonDes": "erreur simulée"}))
    with pytest.raises(fc.FusionComputeTaskFailed, match="erreur simulée"):
        client.wait_task("/service/sites/05C9108A/tasks/42", poll_interval=0)

# --------------------------------------------------------------------------
# Écriture cible (§7.5 ②③④⑤⑦, ajouté le 17/09/2026 — HANDOFF.md, socle L2)
# --------------------------------------------------------------------------
def test_create_and_attach_volume_sends_documented_fields(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("POST", "/vms/42/action/createattachvol", FakeResponse(json_data={
        "urn": "urn:sites:1:volumes:78", "uri": "/service/sites/1/volumes/78",
        "taskUrn": "service:site:1:task:1", "taskUri": "service/sites/1/tasks/1",
    }))

    out = client.create_and_attach_volume(
        "42", name="disk-2000", quantity_gb=80, datastore_urn="urn:sites:1:datastores:1")

    call = next(c for c in session.calls if c["url"].endswith("/action/createattachvol"))
    assert call["json"] == {
        "name": "disk-2000", "quantityGB": 80,
        "datastoreUrn": "urn:sites:1:datastores:1",
        "isThin": True, "type": "normal",
        "indepDisk": True, "persistentDisk": True,
    }
    assert out["urn"] == "urn:sites:1:volumes:78"
    assert out["taskUri"] == "service/sites/1/tasks/1"


def test_create_and_attach_volume_completes_bare_datastore_id(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("POST", "/vms/42/action/createattachvol", FakeResponse(json_data={
        "urn": "u", "uri": "u", "taskUrn": "t", "taskUri": "t"}))

    client.create_and_attach_volume("42", name="d", quantity_gb=1, datastore_urn="5")

    call = next(c for c in session.calls if c["url"].endswith("/action/createattachvol"))
    assert call["json"]["datastoreUrn"] == f"urn:sites:{client.site.id}:datastores:5"


def test_attach_volume_defaults_to_virtio(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("POST", "/vms/42/action/attachvol",
              FakeResponse(json_data={"taskUrn": "t1", "taskUri": "u1"}))

    client.attach_volume("42", vol_urn="urn:sites:1:volumes:78")

    call = next(c for c in session.calls if c["url"].endswith("/action/attachvol"))
    assert call["json"] == {"volUrn": "urn:sites:1:volumes:78", "pciType": "VIRTIO"}


def test_detach_volume_never_formats(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("POST", "/vms/42/action/detachvol",
              FakeResponse(json_data={"taskUrn": "t1", "taskUri": "u1"}))

    client.detach_volume("42", vol_urn="urn:sites:1:volumes:78")

    call = next(c for c in session.calls if c["url"].endswith("/action/detachvol"))
    assert call["json"] == {"volUrn": "urn:sites:1:volumes:78", "isFormat": False}


def test_create_vm_has_no_disks_and_respects_boot_firmware(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("POST", "/vms", FakeResponse(json_data={
        "urn": "urn:sites:1:vms:99", "uri": "/service/sites/1/vms/99",
        "taskUrn": "t1", "taskUri": "u1",
    }))

    out = client.create_vm(name="srv-app-01", location_urn="urn:sites:1:clusters:1",
                           cpu_count=4, memory_mb=8192, boot_firmware="UEFI",
                           os_type="Windows", os_version=1173)

    call = next(c for c in session.calls if c["url"].endswith("/sites/05C9108A/vms"))
    assert call["json"]["vmConfig"]["disks"] == []
    assert call["json"]["vmConfig"]["properties"]["bootFirmware"] == "UEFI"
    assert call["json"]["vmConfig"]["cpu"] == {"quantity": 4}
    assert call["json"]["vmConfig"]["memory"] == {"quantityMB": 8192}
    assert call["json"]["autoBoot"] is False
    assert call["json"]["osOptions"] == {"osType": "Windows", "osVersion": 1173}
    assert out["urn"] == "urn:sites:1:vms:99"


def test_power_on_sends_no_body(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("POST", "/vms/42/action/start",
              FakeResponse(json_data={"taskUrn": "t1", "taskUri": "u1"}))
    client.power_on("42")
    call = next(c for c in session.calls if c["url"].endswith("/action/start"))
    assert call["json"] == {}


def test_get_vm_returns_location(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("GET", "/sites/05C9108A/vms/12", FakeResponse(json_data={
        "uri": "/service/sites/1/vms/12", "urn": "urn:site:1:vms:12",
        "location": "urn:sites:1:clusters:1", "locationName": "cluster-lab",
    }))
    out = client.get_vm("12")
    assert out["location"] == "urn:sites:1:clusters:1"


def test_power_off_defaults_to_safe_mode(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("POST", "/vms/42/action/stop",
              FakeResponse(json_data={"taskUrn": "t1", "taskUri": "u1"}))
    client.power_off("42")
    call = next(c for c in session.calls if c["url"].endswith("/action/stop"))
    assert call["json"] == {"mode": "safe"}



def test_guest_disk_path_uses_truncated_volume_uuid():
    # Constaté sur le VRM réel (26/10/2026) : série virtio = 20 premiers caractères.
    from datamover.fusioncompute import guest_disk_path
    assert guest_disk_path("61172379-913f-4377-a541-eaec968d7e55") == \
        "/dev/disk/by-id/virtio-61172379-913f-4377-a"


def test_list_vms_pages_until_total(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("GET", "/sites/05C9108A/vms", FakeResponse(json_data={
        "total": 2, "vms": [{"uri": "/service/sites/05C9108A/vms/i-1", "uuid": "u"}]}))

    out = client.list_vms(page_size=1)

    offsets = [c["params"]["offset"] for c in session.calls if c["url"].endswith("/vms")]
    assert offsets == [0, 1]
    assert len(out) == 2


def test_error_detail_translates_forced_password_change():
    # Code vu le 26/10/2026 sur un compte d'interconnexion neuf.
    from datamover.fusioncompute import _error_detail
    resp = FakeResponse(status_code=400, json_data={
        "errorCode": "10000041", "errorDes": "为保证账号口令安全，请修改密码。"})
    assert "changement du mot de passe" in _error_detail(resp)


# Extrait de GET /vms/osversions du VRM réel (26/10/2026).
_OS_VERSIONS = {
    "windows": [
        {"id": 202, "versionDes": "Other Windows(64 bit)", "osBit": 64},
        {"id": 1028, "versionDes": "Windows 10 Enterprise 2016 LTSB 64bit", "osBit": 64},
        {"id": 1173, "versionDes": "Windows Server 2016 Datacenter 64bit", "osBit": 64},
        {"id": 1230, "versionDes": "Windows Server 2008 DataCenter R2 SP1 64bit", "osBit": 64},
    ],
    "linux": [
        {"id": 302, "versionDes": "Other Linux(64 bit)", "osBit": 64},
        {"id": 183, "versionDes": "CentOS 6.5 64bit", "osBit": 64},
        {"id": 1202, "versionDes": "CentOS 7.6 64bit", "osBit": 64},
    ],
}


def test_pick_os_version_matches_real_source_vms():
    from datamover.fusioncompute import pick_os_version
    # R00vSkypeEd et R00vSkype-OVOC, guestFullName vSphere réels
    assert pick_os_version(_OS_VERSIONS, "Microsoft Windows Server 2016 or later (64-bit)",
                           "windows9Server64Guest")[:2] == ("Windows", 1173)
    assert pick_os_version(_OS_VERSIONS, "CentOS 4/5/6 (64-bit)", "centos64Guest")[:2] \
        == ("Linux", 183)


def test_pick_os_version_falls_back_to_other():
    from datamover.fusioncompute import pick_os_version
    assert pick_os_version(_OS_VERSIONS, "FreeBSD 13 (64-bit)", "freebsd13_64Guest")[:2] \
        == ("Linux", 302)


def test_delete_volume_returns_task(monkeypatch):
    session = FakeSession()
    client = make_client(monkeypatch, session)
    session.on("DELETE", "/sites/05C9108A/volumes/70",
               FakeResponse(json_data={"taskUri": "/service/sites/05C9108A/tasks/1"}))
    assert client.delete_volume("70")["taskUri"].endswith("/tasks/1")
