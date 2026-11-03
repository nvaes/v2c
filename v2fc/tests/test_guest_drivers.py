"""Lecture des initramfs et choix du noyau (datamover/guest_drivers.py)."""
from __future__ import annotations

import gzip
import lzma
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datamover import guest_drivers as gdm
from datamover.guest_drivers import _initrd_modules, _version_key


def cpio(files: dict[str, bytes]) -> bytes:
    """Archive cpio « newc » minimale."""
    out = b""
    for name, data in [*files.items(), ("TRAILER!!!", b"")]:
        n = name.encode() + b"\0"
        hdr = b"070701" + b"".join(b"%08X" % v for v in
                                   (0, 0o100644, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(n), 0))
        out += hdr + n
        out += b"\0" * (-len(out) % 4) + data
        out += b"\0" * (-len(out) % 4)
    return out


MODULE = "usr/lib/modules/4.18.0/kernel/drivers/block/virtio_blk.ko.xz"


def test_initramfs_gzip():
    assert "virtio_blk" in _initrd_modules(gzip.compress(cpio({MODULE: b"x"})))


def test_microcode_non_compresse_puis_xz():
    early = cpio({"kernel/x86/microcode/GenuineIntel.bin": b"\x01" * 37})
    main = lzma.compress(cpio({MODULE: b"x"}))
    assert "virtio_blk" in _initrd_modules(early + b"\0" * 512 + main)


def test_modules_dep_ne_compte_pas():
    # modules.dep cite virtio_blk.ko, mais le module n'est pas dans l'archive
    dep = b"kernel/drivers/block/virtio_blk.ko.xz: kernel/drivers/virtio/virtio.ko.xz\n"
    img = gzip.compress(cpio({"usr/lib/modules/4.18.0/modules.dep": dep}))
    assert "virtio_blk" not in _initrd_modules(img)


def test_noyau_le_plus_recent():
    kernels = ["4.18.0-553.el8_10.x86_64", "4.18.0-553.179.1.el8_10.x86_64",
               "4.18.0-553.178.1.el8_10.x86_64"]
    assert max(kernels, key=_version_key) == "4.18.0-553.179.1.el8_10.x86_64"


class FakeGuest:
    """Système de fichiers invité minimal pour _pin_interface_names."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)

    def glob_expand(self, pattern: str) -> list[str]:
        import fnmatch
        return sorted(p for p in self.files if fnmatch.fnmatch(p, pattern))

    def is_file(self, path: str) -> bool:
        return path in self.files

    def cat(self, path: str) -> str:
        return self.files[path]

    def write(self, path: str, data: bytes) -> None:
        self.files[path] = data.decode()


IFCFG = "/etc/sysconfig/network-scripts/ifcfg-"


def test_noms_fixes_par_mac_dans_lordre_des_slots():
    g = FakeGuest({IFCFG + "ens192": "DEVICE=ens192\n", IFCFG + "ens160": "DEVICE=ens160\n",
                   IFCFG + "lo": "DEVICE=lo\n"})
    msg = gdm._pin_interface_names(g, [
        {"device_key": 4000, "mac": "00:50:56:AA:00:01", "keep_mac": True},
        {"device_key": 4001, "mac": "00:50:56:aa:00:02", "keep_mac": True}])
    rule = g.files[gdm._UDEV_RULE]
    assert 'ATTR{address}=="00:50:56:aa:00:01", NAME="ens160"' in rule
    assert 'ATTR{address}=="00:50:56:aa:00:02", NAME="ens192"' in rule
    assert "ens160, ens192" in msg


def test_mac_non_conservee():
    g = FakeGuest({IFCFG + "ens160": "DEVICE=ens160\n"})
    msg = gdm._pin_interface_names(g, [{"device_key": 4000, "mac": "00:50:56:aa:00:01",
                                        "keep_mac": False}])
    assert gdm._UDEV_RULE not in g.files
    assert "garder la MAC" in msg


def test_nombre_de_cartes_different():
    g = FakeGuest({IFCFG + "ens160": "DEVICE=ens160\n", IFCFG + "ens192": "DEVICE=ens192\n"})
    msg = gdm._pin_interface_names(g, [{"device_key": 4000, "mac": "x", "keep_mac": True}])
    assert gdm._UDEV_RULE not in g.files and "non fixés" in msg


def test_eth0_non_fixe():
    g = FakeGuest({IFCFG + "eth0": "DEVICE=eth0\n"})
    assert gdm._pin_interface_names(g, [{"device_key": 4000, "mac": "x", "keep_mac": True}]) == ""


class FakeWindows:
    """Windows hors ligne : fichiers, DriverStore et ruche SYSTEM
    (Services\\<nom>\\Start) réduits au strict nécessaire pour `_windows`."""

    def __init__(self, sys_files=(), staged=(), services=None):
        self.sys_files = {f"/Windows/System32/drivers/{n}.sys" for n in sys_files}
        self.staged = [f"{n}.inf_amd64_0123" for n in staged]
        # noeuds : 1 racine, 2 Select, 3 ControlSet001, 4 Services, 10+ services
        self.children = {(1, "Select"): 2, (1, "ControlSet001"): 3, (3, "Services"): 4}
        self.values = {(2, "Current"): 1}
        for i, (name, start) in enumerate((services or {}).items()):
            self.children[(4, name)] = 10 + i
            self.values[(10 + i, "Start")] = start

    def inspect_get_windows_systemroot(self, root):
        return "/Windows"

    def case_sensitive_path(self, path):
        return path

    def ls(self, path):
        return self.staged

    def is_file(self, path):
        return path in self.sys_files

    def hivex_open(self, path, write):
        pass

    def hivex_close(self):
        pass

    def hivex_root(self):
        return 1

    def hivex_node_get_child(self, node, name):
        return self.children.get((node, name), 0)

    def hivex_node_get_value(self, node, name):
        return (node, name) if (node, name) in self.values else 0

    def hivex_value_value(self, value):
        return struct.pack("<I", self.values[value])


def _windows_result(g):
    result = gdm._new_result()
    gdm._windows(g, "/dev/sda2", result)
    return result


def test_windows_netkvm_dans_le_driverstore_suffit():
    # virtio-win installé sur VMware : stockage actif, netkvm sans carte virtio
    r = _windows_result(FakeWindows(sys_files=["viostor"], staged=["viostor", "netkvm"],
                                    services={"viostor": 0}))
    assert (r["storage"], r["network"]) == ("ok", "ok")
    assert any("Plug and Play" in d for d in r["details"])


def test_windows_sans_netkvm():
    r = _windows_result(FakeWindows(sys_files=["viostor"], services={"viostor": 0}))
    assert r["network"] == "absent"


def test_windows_viostor_seulement_dans_le_driverstore():
    r = _windows_result(FakeWindows(staged=["viostor"]))
    assert r["storage"] == "inactive"
