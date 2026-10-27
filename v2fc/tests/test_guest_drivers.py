"""Lecture des initramfs et choix du noyau (datamover/guest_drivers.py)."""
from __future__ import annotations

import gzip
import lzma
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datamover.guest_drivers import _initrd_text, _version_key


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
    assert b"virtio_blk.ko" in _initrd_text(gzip.compress(cpio({MODULE: b"x"})))


def test_microcode_non_compresse_puis_xz():
    early = cpio({"kernel/x86/microcode/GenuineIntel.bin": b"\x01" * 37})
    main = lzma.compress(cpio({MODULE: b"x"}))
    assert b"virtio_blk.ko" in _initrd_text(early + b"\0" * 512 + main)


def test_initramfs_sans_virtio():
    assert b"virtio_blk.ko" not in _initrd_text(gzip.compress(cpio({"init": b"#!/bin/sh"})))


def test_noyau_le_plus_recent():
    kernels = ["4.18.0-553.el8_10.x86_64", "4.18.0-553.179.1.el8_10.x86_64",
               "4.18.0-553.178.1.el8_10.x86_64"]
    assert max(kernels, key=_version_key) == "4.18.0-553.179.1.el8_10.x86_64"
