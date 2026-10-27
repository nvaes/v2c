"""Lecture des initramfs et choix du noyau (datamover/guest_drivers.py)."""
from __future__ import annotations

import gzip
import lzma
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
