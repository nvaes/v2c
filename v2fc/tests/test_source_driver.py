"""
Tests du driver source, sans vCenter.

Les objets de périphérique sont de vrais objets de données pyVmomi — c'est
indispensable, le driver s'appuie sur isinstance() pour distinguer disques et
RDM. Seuls les objets managés (VirtualMachine, snapshot) sont simulés, parce
qu'ils exigeraient un stub SOAP.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from pyVmomi import vim, vmodl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datamover import extents as ex
from datamover.preflight import Severity, run_preflight
from datamover.vsphere import (CbtInvalidated, DiskSpec, SourceGuard,
                               SourceMutationRefused, SourceVm)

GIB = 1024 ** 3


# --------------------------------------------------------------------------
# Fabriques
# --------------------------------------------------------------------------
def make_disk(key=2000, unit=0, controller=1000, capacity=40 * GIB,
              mode="persistent", sharing=None, rdm=None, change_id=None,
              name="[ds1] VM/VM.vmdk"):
    d = vim.vm.device.VirtualDisk()
    d.key, d.unitNumber, d.controllerKey = key, unit, controller
    d.capacityInBytes = capacity
    d.deviceInfo = vim.Description(label=f"Hard disk {unit + 1}", summary="")
    if rdm:
        b = vim.vm.device.VirtualDisk.RawDiskMappingVer1BackingInfo()
        b.compatibilityMode = rdm
    else:
        b = vim.vm.device.VirtualDisk.FlatVer2BackingInfo()
        b.thinProvisioned = True
    b.fileName = name
    b.diskMode = mode
    if sharing:
        b.sharing = sharing
    if change_id:
        b.changeId = change_id
    d.backing = b
    return d


def make_vm(devices=None, *, name="VM-TEST", cbt=True, template=False,
            firmware="bios", encrypted=False, tools="toolsOk",
            power="poweredOn", snapshots=(), guest="rhel9_64Guest",
            vmx="[ds1] VM-TEST/VM-TEST.vmx", managed_by=None):
    ns = types.SimpleNamespace
    cfg = ns(template=template, changeTrackingEnabled=cbt, firmware=firmware,
             guestId=guest, instanceUuid="502f-aaaa-bbbb", keyId="k" if encrypted else None,
             files=ns(vmPathName=vmx), managedBy=managed_by,
             hardware=ns(device=list(devices or [make_disk()])))
    snaps = [ns(name=n, snapshot=object(), childSnapshotList=[]) for n in snapshots]
    vm = ns(name=name, _moId="vm-42", config=cfg,
            runtime=ns(powerState=power), guest=ns(toolsStatus=tools),
            snapshot=ns(rootSnapshotList=snaps) if snaps else None,
            parent=None)
    return vm


def _bare(vm):
    """SourceVm sans client : aucun test ici n'émet d'appel réseau."""
    obj = SourceVm.__new__(SourceVm)
    obj.client = None
    obj.raw = vm
    obj.guarded = SourceGuard(vm)
    return obj


# --------------------------------------------------------------------------
# Invariant I1 — le garde-fou
# --------------------------------------------------------------------------
class TestSourceGuard:
    def test_destruction_refusee(self):
        vm = make_vm()
        vm.Destroy_Task = lambda: pytest.fail("ne doit jamais être appelée")
        with pytest.raises(SourceMutationRefused, match="invariant I1"):
            SourceGuard(vm).Destroy_Task

    @pytest.mark.parametrize("method", [
        "Destroy_Task", "UnregisterVM", "RelocateVM_Task", "MigrateVM_Task",
        "CloneVM_Task", "MarkAsTemplate", "RevertToSnapshot_Task",
        "RemoveAllSnapshots_Task", "PowerOnVM_Task",
    ])
    def test_methodes_destructrices(self, method):
        vm = make_vm()
        setattr(vm, method, lambda *a, **k: None)
        with pytest.raises(SourceMutationRefused):
            getattr(SourceGuard(vm), method)

    def test_mutation_non_listee_refusee(self):
        vm = make_vm()
        vm.UpgradeVM_Task = lambda *a, **k: None
        with pytest.raises(SourceMutationRefused, match="liste blanche"):
            SourceGuard(vm).UpgradeVM_Task

    def test_mutations_autorisees_passent(self):
        vm = make_vm()
        for m in ("CreateSnapshot_Task", "RemoveSnapshot_Task",
                  "ReconfigVM_Task", "ShutdownGuest", "PowerOffVM_Task"):
            setattr(vm, m, lambda *a, **k: "ok")
        g = SourceGuard(vm)
        for m in ("CreateSnapshot_Task", "RemoveSnapshot_Task",
                  "ReconfigVM_Task", "ShutdownGuest", "PowerOffVM_Task"):
            assert callable(getattr(g, m))

    def test_lectures_passent(self):
        g = SourceGuard(make_vm(name="LECTURE"))
        assert g.name == "LECTURE"
        assert g.config.firmware == "bios"

    def test_ecriture_directe_refusee(self):
        with pytest.raises(SourceMutationRefused):
            SourceGuard(make_vm()).name = "autre"


# --------------------------------------------------------------------------
# Lecture du modèle
# --------------------------------------------------------------------------
class TestModele:
    def test_vmx_spec(self, monkeypatch):
        vm = _bare(make_vm(vmx="[datastore1] MaVM/MaVM.vmx"))
        monkeypatch.setattr(type(vm), "datacenter_path",
                            property(lambda self: "Prod/DC-Liege"))
        assert vm.vmx_spec == "MaVM/MaVM.vmx?dcPath=Prod/DC-Liege&dsName=datastore1"

    def test_vmx_spec_malforme(self, monkeypatch):
        vm = _bare(make_vm(vmx="chemin-sans-datastore.vmx"))
        monkeypatch.setattr(type(vm), "datacenter_path", property(lambda s: "DC"))
        with pytest.raises(Exception, match="vmPathName"):
            _ = vm.vmx_spec

    def test_disques_tries_par_controleur_et_unite(self):
        vm = _bare(make_vm([
            make_disk(key=2002, unit=2, controller=1000),
            make_disk(key=2000, unit=0, controller=1000),
            make_disk(key=3000, unit=0, controller=1001),
        ]))
        got = [(d.controller_key, d.unit_number) for d in vm.disks()]
        assert got == [(1000, 0), (1000, 2), (1001, 0)]

    def test_detection_rdm(self):
        vm = _bare(make_vm([make_disk(rdm="physicalMode")]))
        d = vm.disks()[0]
        assert d.is_rdm and d.is_physical_rdm

    def test_change_id_remonte(self):
        vm = _bare(make_vm([make_disk(change_id="52 aa bb/17")]))
        assert vm.disks()[0].change_id == "52 aa bb/17"


# --------------------------------------------------------------------------
# Continuité du CBT
# --------------------------------------------------------------------------
class TestCbt:
    def test_capacite_changee_invalide_le_change_id(self):
        vm = _bare(make_vm())
        disk = vm.disks()[0]
        with pytest.raises(CbtInvalidated, match="capacité"):
            vm.validate_cbt_continuity(disk, expected_capacity=20 * GIB)

    def test_capacite_stable_passe(self):
        vm = _bare(make_vm())
        disk = vm.disks()[0]
        vm.validate_cbt_continuity(disk, expected_capacity=40 * GIB)

    def test_faute_vsphere_convertie_en_cbt_invalide(self):
        vm = _bare(make_vm())
        disk = vm.disks()[0]

        def boom(**kwargs):
            raise vmodl.fault.NotSupported(msg="CBT non supporté")
        vm.raw.QueryChangedDiskAreas = boom
        with pytest.raises(CbtInvalidated, match="Re-seed complet obligatoire"):
            vm.query_changed_areas(object(), disk, "52 aa/3")

    def test_boucle_de_couverture_complete(self):
        """vSphere répond par morceaux : la boucle doit tout couvrir."""
        cap = 40 * GIB
        moitie = cap // 2
        appels = []

        def query(offset):
            appels.append(offset)
            if offset == 0:
                return types.SimpleNamespace(
                    startOffset=0, length=moitie,
                    changedArea=[types.SimpleNamespace(start=0, length=1 << 20)])
            return types.SimpleNamespace(
                startOffset=moitie, length=cap - moitie,
                changedArea=[types.SimpleNamespace(start=moitie, length=1 << 20)])

        res = ex.query_changed_areas_complete(query, cap, "52 aa/3")
        assert appels == [0, moitie]
        assert sum(e.length for e in res) == 2 << 20

    def test_absence_de_progression_detectee(self):
        """Une réponse qui n'avance pas boucherait à l'infini : on lève."""
        def query(offset):
            return types.SimpleNamespace(startOffset=0, length=0, changedArea=[])
        with pytest.raises(ValueError, match="n'avance pas"):
            ex.query_changed_areas_complete(query, 40 * GIB, "52 aa/3")

    def test_couverture_partielle_refusee(self):
        """Ne jamais accepter un disque partiellement couvert."""
        cap = 40 * GIB

        def query(offset):
            return types.SimpleNamespace(startOffset=0, length=cap // 4,
                                         changedArea=[])
        with pytest.raises(ValueError, match="couverture incomplète|n'avance pas"):
            ex.query_changed_areas_complete(query, cap, "52 aa/3")


# --------------------------------------------------------------------------
# Pré-vérifications
# --------------------------------------------------------------------------
def codes(rep, sev=None):
    return {f.code for f in rep.findings if sev is None or f.severity is sev}


class TestPreflight:
    def test_vm_saine_passe(self):
        rep = run_preflight(_bare(make_vm()))
        assert rep.ok, rep.render()
        assert not rep.blockers

    def test_disque_independant_bloque(self):
        rep = run_preflight(_bare(make_vm([
            make_disk(mode="independent_persistent")])))
        assert not rep.ok
        assert "DISQUE_INDEPENDANT" in codes(rep, Severity.BLOCK)

    def test_rdm_physique_bloque(self):
        rep = run_preflight(_bare(make_vm([make_disk(rdm="physicalMode")])))
        assert "RDM_PHYSIQUE" in codes(rep, Severity.BLOCK)

    def test_rdm_virtuel_avertit_seulement(self):
        rep = run_preflight(_bare(make_vm([make_disk(rdm="virtualMode")])))
        assert rep.ok
        assert "RDM_VIRTUEL" in codes(rep, Severity.WARN)

    def test_multi_writer_bloque(self):
        rep = run_preflight(_bare(make_vm([
            make_disk(sharing="sharingMultiWriter")])))
        assert "MULTI_WRITER" in codes(rep, Severity.BLOCK)

    def test_vm_chiffree_bloque(self):
        rep = run_preflight(_bare(make_vm(encrypted=True)))
        assert "VM_CHIFFREE" in codes(rep, Severity.BLOCK)

    def test_template_bloque(self):
        rep = run_preflight(_bare(make_vm(template=True)))
        assert "TEMPLATE" in codes(rep, Severity.BLOCK)

    def test_cbt_inactif_avertit(self):
        rep = run_preflight(_bare(make_vm(cbt=False)))
        assert rep.ok
        assert "CBT_INACTIF" in codes(rep, Severity.WARN)

    def test_tools_absent_avertit(self):
        rep = run_preflight(_bare(make_vm(tools="toolsNotInstalled")))
        assert "TOOLS_ABSENT" in codes(rep, Severity.WARN)

    def test_snapshots_etrangers_avertissent(self):
        rep = run_preflight(_bare(make_vm(snapshots=["avant-patch", "v2fc-r3"])))
        warn = [f for f in rep.warnings if f.code == "SNAPSHOTS_EXISTANTS"]
        assert warn and "avant-patch" in warn[0].message
        assert "v2fc-r3" not in warn[0].message      # les nôtres ne comptent pas

    def test_capacite_non_alignee_bloque(self):
        rep = run_preflight(_bare(make_vm([make_disk(capacity=40 * GIB + 7)])))
        assert "CAPACITE_NON_ALIGNEE" in codes(rep, Severity.BLOCK)

    def test_firmware_efi_signale(self):
        rep = run_preflight(_bare(make_vm(firmware="efi")))
        info = [f for f in rep.findings if f.code == "FIRMWARE"]
        assert info and "EFI" in info[0].message

    def test_volumetrie_exclut_les_disques_bloquants(self):
        rep = run_preflight(_bare(make_vm([
            make_disk(key=2000, unit=0, capacity=40 * GIB),
            make_disk(key=2001, unit=1, capacity=100 * GIB,
                      mode="independent_persistent"),
        ])))
        vol = next(f for f in rep.findings if f.code == "VOLUMETRIE")
        assert "40.0 Gio" in vol.message          # les 100 Gio bloqués sont exclus

    def test_rendu_lisible(self):
        rep = run_preflight(_bare(make_vm([make_disk(rdm="physicalMode")])))
        txt = rep.render()
        assert "MIGRATION REFUSÉE" in txt
        assert "RDM_PHYSIQUE" in txt


# --------------------------------------------------------------------------
# Chemin de datacenter — vCenter imbriqué et ESXi autonome
# --------------------------------------------------------------------------
class _FakeDC(vim.Datacenter):
    """Sous-classe de vim.Datacenter pour satisfaire isinstance() sans stub SOAP."""

    def __init__(self, name, parent):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_parent", parent)

    @property
    def name(self):
        return object.__getattribute__(self, "_name")

    @property
    def parent(self):
        return object.__getattribute__(self, "_parent")


class TestDatacenterPath:
    def test_esxi_autonome(self):
        """ESXi seul : un datacenter implicite, directement sous la racine."""
        ns = types.SimpleNamespace
        root = ns(name="ha-folder-root", parent=None)
        dc = _FakeDC("ha-datacenter", root)
        vm = make_vm()
        vm.parent = ns(name="ha-folder-vm", parent=dc)
        assert _bare(vm).datacenter_path == "ha-datacenter"

    def test_vcenter_avec_dossiers(self):
        ns = types.SimpleNamespace
        root = ns(name="Datacenters", parent=None)
        prod = ns(name="Prod", parent=root)
        dc = _FakeDC("DC-Liege", prod)
        vm = make_vm()
        vm.parent = ns(name="vm", parent=dc)
        assert _bare(vm).datacenter_path == "Prod/DC-Liege"

    def test_vcenter_sans_dossier(self):
        ns = types.SimpleNamespace
        root = ns(name="Datacenters", parent=None)
        dc = _FakeDC("DC-01", root)
        vm = make_vm()
        vm.parent = ns(name="vm", parent=dc)
        assert _bare(vm).datacenter_path == "DC-01"

    def test_vmx_spec_complet_esxi(self):
        ns = types.SimpleNamespace
        root = ns(name="ha-folder-root", parent=None)
        dc = _FakeDC("ha-datacenter", root)
        vm = make_vm(vmx="[datastore1] TEST/TEST.vmx")
        vm.parent = ns(name="vm", parent=dc)
        assert _bare(vm).vmx_spec == \
            "TEST/TEST.vmx?dcPath=ha-datacenter&dsName=datastore1"

    def test_datacenter_absent_leve(self):
        ns = types.SimpleNamespace
        vm = make_vm()
        vm.parent = ns(name="orphelin", parent=None)
        with pytest.raises(Exception, match="datacenter"):
            _ = _bare(vm).datacenter_path


# --------------------------------------------------------------------------
# VM d'agent et connexion directe à l'hôte — constats du relevé ESXi 8.0.3
# --------------------------------------------------------------------------
def _with_client(vm, api_type):
    ns = types.SimpleNamespace
    obj = _bare(vm)
    obj.client = ns(content=ns(about=ns(apiType=api_type)))
    return obj


class TestVmAgent:
    def test_vcls_bloquee(self):
        """Une VM vCLS porte config.managedBy : elle ne se migre pas."""
        ns = types.SimpleNamespace
        vm = make_vm(devices=[make_disk()],
                     managed_by=ns(extensionKey="com.vmware.vim.eam",
                                   type="cluster-agent"))
        rep = run_preflight(_bare(vm))
        assert not rep.ok
        blockers = {f.code: f for f in rep.blockers}
        assert "VM_AGENT" in blockers
        assert "com.vmware.vim.eam" in blockers["VM_AGENT"].message

    def test_vm_ordinaire_non_bloquee(self):
        assert run_preflight(_bare(make_vm())).ok

    def test_managed_by_sans_type(self):
        ns = types.SimpleNamespace
        vm = make_vm(managed_by=ns(extensionKey="com.vmware.vcls", type=None))
        assert _bare(vm).managed_by == "com.vmware.vcls"


class TestConnexionHote:
    def test_esxi_direct_avertit(self):
        rep = run_preflight(_with_client(make_vm(), "HostAgent"))
        assert rep.ok                       # avertissement, pas blocage
        assert "CONNEXION_HOTE" in {f.code for f in rep.warnings}

    def test_vcenter_nadvertit_pas(self):
        rep = run_preflight(_with_client(make_vm(), "VirtualCenter"))
        assert "CONNEXION_HOTE" not in {f.code for f in rep.warnings}

    def test_sans_client_nadvertit_pas(self):
        rep = run_preflight(_bare(make_vm()))
        assert "CONNEXION_HOTE" not in {f.code for f in rep.warnings}


# --------------------------------------------------------------------------
# Régression : disks(snapshot=…) accepte le Snapshot ou son ConfigInfo
# --------------------------------------------------------------------------
class TestDisquesDepuisSnapshot:
    """
    Le premier cycle de mesure sur l'ESXi a planté sur un AttributeError :
    l'appelant passait snapshot.config là où disks() attendait le snapshot.
    Les deux formes sont désormais acceptées.
    """

    def _snapshot_mo(self, devices):
        ns = types.SimpleNamespace
        return ns(config=ns(hardware=ns(device=devices)))

    def test_depuis_le_snapshot_manage(self):
        d = make_disk(change_id="52 11 22/9", name="[DS] ubuntu/ubuntu.vmdk")
        vm = _bare(make_vm())
        got = vm.disks(snapshot=self._snapshot_mo([d]))
        assert len(got) == 1
        assert got[0].change_id == "52 11 22/9"
        assert got[0].file_name == "[DS] ubuntu/ubuntu.vmdk"

    def test_depuis_le_config_info(self):
        d = make_disk(change_id="52 11 22/9")
        vm = _bare(make_vm())
        snap = self._snapshot_mo([d])
        assert vm.disks(snapshot=snap.config)[0].change_id == "52 11 22/9"

    def test_les_deux_formes_concordent(self):
        d = make_disk(change_id="52 aa/4")
        vm = _bare(make_vm())
        snap = self._snapshot_mo([d])
        assert vm.disks(snapshot=snap) == vm.disks(snapshot=snap.config)

    def test_objet_inattendu_leve(self):
        vm = _bare(make_vm())
        with pytest.raises(Exception, match="snapshot inattendu"):
            vm.disks(snapshot=object())

    def test_sans_snapshot_lit_la_vm(self):
        vm = _bare(make_vm([make_disk(change_id="courant")]))
        assert vm.disks()[0].change_id == "courant"


class TestAgentSansManagedBy:
    """
    Sur l'ESXi de test, la VM vCLS ne porte AUCUN managedBy vu depuis une
    session hôte : la propriété vient de vCenter. Le type d'invité reste
    révélateur.
    """

    def test_crx_detecte_sans_managed_by(self):
        vm = make_vm(devices=[make_disk()], guest="crxSys1Guest",
                     name="vCLS-3841ede5-dbca-4ade-9e71-341137fdaf47")
        src = _bare(vm)
        assert src.managed_by is None
        assert "crxSys1Guest" in src.agent_signal
        rep = run_preflight(src)
        assert "VM_AGENT" in {f.code for f in rep.blockers}

    def test_nom_vcls_detecte(self):
        vm = make_vm(devices=[make_disk()], guest="other3xLinux64Guest",
                     name="vCLS-abcdef")
        assert "nom réservé" in _bare(vm).agent_signal

    def test_vm_ordinaire_non_detectee(self):
        src = _bare(make_vm(name="ubuntu", guest="ubuntu64Guest"))
        assert src.agent_signal is None
        assert run_preflight(src).ok


# --------------------------------------------------------------------------
# Progression du round (job-run -> barre de progression, 26/10/2026)
# --------------------------------------------------------------------------
def test_round_progress_reports_start_resume_offset_and_end(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import v2fc_source
    calls = []
    clock = iter([0.0, 1.0, 10.0, 10.0, 12.0, 12.0])
    monkeypatch.setattr(v2fc_source.time, "time", lambda: next(clock))

    p = v2fc_source._RoundProgress(100, 40, lambda planned, done: calls.append((planned, done)))
    p.update(10)      # < 5 s depuis le dernier envoi : rien
    p.update(10)      # 10 s : envoi
    p.update(40)      # fin du round : envoi immédiat
    assert calls == [(100, 40), (100, 60), (100, 100)]
