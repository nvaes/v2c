"""
Pilotes virtio de l'OS invité (27/10/2026) : présents ou non sur la copie de
la VM, lue hors ligne sur les volumes cibles attachés au relais, à la fin de
chaque round réussi (job-run).

Une VM venue de VMware démarre sur un contrôleur LSI/PVSCSI : sans pilote
virtio de stockage chargé au démarrage, Windows tombe en
INACCESSIBLE_BOOT_DEVICE et Linux ne trouve pas sa racine sur un disque
VIRTIO FusionCompute. Les disques sont donc attachés en IDE
(api/routers/jobs.py, `_TARGET_PCI_TYPE`) ; ce module dit si VIRTIO est
possible.

Lecture seule, par libguestfs (appliance en émulation TCG sur le relais,
sans /dev/kvm : ~30 s). Le volume ne change qu'au round suivant, que le
runner ne lance pas pendant cette inspection.

Résultat :
    storage : ok        pilote présent et chargé au démarrage
              inactive  pilote présent mais pas au démarrage (Windows :
                        Start ≠ 0 ou seulement dans le DriverStore ; Linux :
                        module absent de l'initramfs)
              absent    aucun pilote
              unknown   inspection impossible (détail dans `details`)
    network : ok | absent | unknown   (chargé après le démarrage : pas de
                                       contrainte d'initramfs)
"""
from __future__ import annotations

import logging
import lzma
import re
import shutil
import struct
import subprocess
import zlib
from datetime import datetime, timezone

log = logging.getLogger("v2fc.guest")

# Windows : service -> rôle. viostor = virtio-blk (type VIRTIO de
# FusionCompute), vioscsi = virtio-scsi.
_WIN_STORAGE = ("viostor", "vioscsi")
_WIN_NETWORK = ("netkvm",)
_LINUX_STORAGE = ("virtio_blk", "virtio_scsi")
_LINUX_NETWORK = ("virtio_net",)


def inspect(devices: list[str]) -> dict:
    """Inspecte les disques d'une VM (tous, dans l'ordre : un volume LVM peut
    s'étendre sur plusieurs). Ne lève pas : `unknown` avec la raison."""
    result = {"os": None, "product": None, "storage": "unknown", "network": "unknown",
              "details": [], "inspected_at": datetime.now(timezone.utc).isoformat()}
    try:
        import guestfs
    except ImportError:
        result["details"].append("libguestfs absent de l'image du runner")
        return result

    g = guestfs.GuestFS(python_return_dict=True)
    try:
        for dev in devices:
            g.add_drive_opts(dev, readonly=1, format="raw")
        g.launch()
        roots = g.inspect_os()
        if not roots:
            result["details"].append("aucun système d'exploitation trouvé sur les disques")
            return result
        root = roots[0]
        result["os"] = g.inspect_get_type(root)
        result["product"] = g.inspect_get_product_name(root)
        mounts = g.inspect_get_mountpoints(root)
        for mp in sorted(mounts, key=len):
            _mount_ro(g, mounts[mp], mp, result)
        if result["os"] == "windows":
            _windows(g, root, result)
        elif result["os"] == "linux":
            _linux(g, result)
        else:
            result["details"].append(f"OS {result['os']} non pris en charge")
    except RuntimeError as exc:
        result["details"].append(f"inspection impossible : {exc}")
    finally:
        try:
            g.shutdown()
        except RuntimeError:
            pass
        g.close()
    return result


def _mount_ro(g, dev: str, mp: str, result: dict) -> None:
    """Copie prise à chaud (snapshot sans quiesce) : journal XFS/ext4 non
    rejoué, refusé par un montage simple — on le monte sans le rejouer."""
    fstype = g.vfs_type(dev)
    options = {"xfs": "ro,norecovery", "ext3": "ro,noload", "ext4": "ro,noload"}
    try:
        g.mount_ro(dev, mp)
        return
    except RuntimeError as exc:
        if fstype not in options:
            result["details"].append(f"montage de {mp} impossible : {exc}")
            return
    try:
        g.mount_options(options[fstype], dev, mp)
    except RuntimeError as exc:
        result["details"].append(f"montage de {mp} impossible : {exc}")


# -- Windows ---------------------------------------------------------------
def _hive_dword(g, node: int, name: str) -> int | None:
    try:
        value = g.hivex_node_get_value(node, name)
    except RuntimeError:
        return None
    if not value:
        return None
    raw = g.hivex_value_value(value)
    return struct.unpack("<I", raw[:4])[0] if len(raw) >= 4 else None


def _hive_child(g, node: int, name: str) -> int | None:
    try:
        child = g.hivex_node_get_child(node, name)
    except RuntimeError:
        return None
    return child or None


def _windows(g, root: str, result: dict) -> None:
    systemroot = g.inspect_get_windows_systemroot(root)
    drivers_dir = f"{systemroot}/System32/drivers"
    staged = set()
    try:
        repo = g.case_sensitive_path(f"{systemroot}/System32/DriverStore/FileRepository")
        for entry in g.ls(repo):
            staged.add(entry.lower().split(".inf_", 1)[0])
    except RuntimeError:
        pass

    def has_file(service: str) -> bool:
        try:
            return g.is_file(g.case_sensitive_path(f"{drivers_dir}/{service}.sys"))
        except RuntimeError:
            return False

    services = {}
    hive = g.case_sensitive_path(f"{systemroot}/System32/config/SYSTEM")
    g.hivex_open(hive, write=False)
    try:
        hroot = g.hivex_root()
        select = _hive_child(g, hroot, "Select")
        current = _hive_dword(g, select, "Current") if select else None
        cs = _hive_child(g, hroot, f"ControlSet{current or 1:03d}")
        svc_root = _hive_child(g, cs, "Services") if cs else None
        for name in _WIN_STORAGE + _WIN_NETWORK:
            node = _hive_child(g, svc_root, name) if svc_root else None
            services[name] = _hive_dword(g, node, "Start") if node else None
    finally:
        g.hivex_close()

    storage = "absent"
    for name in _WIN_STORAGE:
        start, present = services[name], has_file(name)
        if present and start == 0:
            storage = "ok"
            result["details"].append(f"{name}.sys installé, chargé au démarrage")
        elif present or start is not None:
            if storage != "ok":
                storage = "inactive"
            result["details"].append(
                f"{name} présent mais pas au démarrage (Start={start}, "
                f"fichier {'présent' if present else 'absent'})")
        elif name in staged:
            if storage == "absent":
                storage = "inactive"
            result["details"].append(f"{name} seulement dans le DriverStore (non installé)")
    result["storage"] = storage
    network = [n for n in _WIN_NETWORK if has_file(n) or services[n] is not None]
    result["network"] = "ok" if network else "absent"
    if not network and any(n in staged for n in _WIN_NETWORK):
        result["details"].append("netkvm seulement dans le DriverStore")


# -- Linux -----------------------------------------------------------------
def _version_key(v: str) -> list[int]:
    # nombres seuls : « 553.el8_10 » < « 553.179.1.el8_10 » (8 < 179)
    return [int(n) for n in re.findall(r"\d+", v)]


def _linux(g, result: dict) -> None:
    try:
        kernels = sorted((k for k in g.ls("/lib/modules") if g.is_dir(f"/lib/modules/{k}")),
                         key=_version_key)
    except RuntimeError:
        kernels = []
    if not kernels:
        result["details"].append("aucun noyau dans /lib/modules")
        return
    kernel = kernels[-1]          # le plus récent : celui que GRUB démarre par défaut
    base = f"/lib/modules/{kernel}"
    builtin = set()
    if g.is_file(f"{base}/modules.builtin"):
        builtin = {line.rsplit("/", 1)[-1].split(".ko")[0]
                   for line in g.cat(f"{base}/modules.builtin").splitlines()}
    on_disk = set()
    if g.is_file(f"{base}/modules.dep"):
        for line in g.cat(f"{base}/modules.dep").splitlines():
            on_disk.add(line.split(":", 1)[0].rsplit("/", 1)[-1].split(".ko")[0])

    initrd = next((p for p in (f"/boot/initramfs-{kernel}.img", f"/boot/initrd.img-{kernel}",
                               f"/boot/initrd-{kernel}") if g.is_file(p)), None)
    initrd_names = None
    if initrd:
        try:
            initrd_names = _initrd_text(g.read_file(initrd))
        except (OSError, ValueError, RuntimeError) as exc:
            result["details"].append(f"initramfs illisible ({initrd}) : {exc}")
    else:
        result["details"].append(f"pas d'initramfs pour le noyau {kernel}")

    storage = "absent"
    for name in _LINUX_STORAGE:
        if name in builtin:
            storage = "ok"
            result["details"].append(f"{name} intégré au noyau {kernel}")
        elif name in on_disk:
            in_initrd = initrd_names is not None and f"{name}.ko".encode() in initrd_names
            if in_initrd:
                storage = "ok"
            elif storage != "ok":
                storage = "inactive" if initrd_names is not None else "unknown"
            result["details"].append(
                f"{name} : module du noyau {kernel}, "
                + ("dans l'initramfs" if in_initrd else
                   "absent de l'initramfs" if initrd_names is not None else
                   "initramfs non vérifié"))
    result["storage"] = storage
    net = [n for n in _LINUX_NETWORK if n in builtin or n in on_disk]
    result["network"] = "ok" if net else "absent"


def _initrd_text(data: bytes) -> bytes:
    """
    Contenu décompressé d'un initramfs, pour y chercher des noms de modules.
    dracut et initramfs-tools préfixent souvent une archive cpio non
    compressée (microcode) avant l'archive principale compressée : on saute
    les archives cpio « newc » non compressées puis on décompresse la suite.
    """
    out = bytearray()
    pos = 0
    while data[pos:pos + 6] in (b"070701", b"070702"):
        start = pos
        while True:
            namesize = int(data[pos + 94:pos + 102], 16)
            filesize = int(data[pos + 54:pos + 62], 16)
            name = data[pos + 110:pos + 110 + namesize - 1]
            pos = (pos + 110 + namesize + 3) & ~3
            pos = (pos + filesize + 3) & ~3
            if name == b"TRAILER!!!":
                break
        out += data[start:pos]
        while pos < len(data) and data[pos] == 0:        # bourrage entre archives
            pos += 1
    rest = data[pos:]
    if not rest:
        return bytes(out)
    # décompresseurs « flux » : le bourrage final après l'archive est ignoré
    if rest[:2] == b"\x1f\x8b":
        out += zlib.decompressobj(wbits=31).decompress(rest)
    elif rest[:6] == b"\xfd7zXZ\x00":
        out += lzma.LZMADecompressor().decompress(rest)
    elif rest[:4] == b"\x28\xb5\x2f\xfd" or rest[:4] == b"\x02\x21\x4c\x18" or rest[:3] == b"BZh":
        tool = {b"\x28": "zstd", b"\x02": "lz4", b"B": "bzip2"}[rest[:1]]
        if shutil.which(tool) is None:
            raise ValueError(f"compression {tool} non prise en charge sur le relais")
        out += subprocess.run([tool, "-dc"], input=rest, capture_output=True,
                              check=True).stdout
    else:
        out += rest
    return bytes(out)
