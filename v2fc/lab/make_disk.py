"""Banc d'essai : crée un VMDK et y écrit un motif connu via le VDDK."""
import hashlib, logging, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datamover import vddk

DISTRIB = os.environ["VDDK_DISTRIB"]
LAB = os.path.dirname(os.path.abspath(__file__))
DISK = os.path.join(LAB, "test.vmdk")
REF  = os.path.join(LAB, "reference.raw")
CAP  = 2 * 1024**3          # 2 Gio

# Régions écrites : on laisse de grands trous pour vérifier que
# QueryAllocatedBlocks ne remonte que ce qui est réellement alloué.
REGIONS = [
    (0,                4 * 1024**2, b"BOOT"),      # début
    (512 * 1024**2,    8 * 1024**2, b"MIDL"),      # milieu
    (1536 * 1024**2,  16 * 1024**2, b"TAIL"),      # 3/4
    (CAP - 1024**2,    1024**2,     b"LAST"),      # dernier Mio
]

def pattern(tag: bytes, offset: int, length: int) -> bytes:
    """Motif déterministe et positionnel : toute erreur d'offset se voit."""
    out = bytearray()
    blk = 4096
    for pos in range(offset, offset + length, blk):
        seed = f"{tag.decode()}:{pos:016x}:".encode()
        body = hashlib.sha256(seed).digest()
        chunk = (seed + body * ((blk // 32) + 1))[:blk]
        out += chunk
    return bytes(out[:length])

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    for p in (DISK, REF):
        if os.path.exists(p):
            os.remove(p)
    for extra in os.listdir(LAB):
        if extra.startswith("test") and extra.endswith(".vmdk"):
            os.remove(os.path.join(LAB, extra))

    env = dict(os.environ, LD_LIBRARY_PATH=f"{DISTRIB}/lib64")
    subprocess.run([f"{DISTRIB}/bin64/vmware-vdiskmanager", "-q", "-c",
                    "-s", f"{CAP // 1024**2}MB", "-a", "lsilogic", "-t", "0", DISK],
                   check=True, env=env, capture_output=True)
    print(f"VMDK créé : {DISK}  ({os.path.getsize(DISK):,} octets sur disque)")

    vddk.init(DISTRIB)
    with vddk.Connection.local(read_only=False) as conn:
        with conn.open(DISK, read_only=False) as disk:
            assert disk.capacity_bytes == CAP, (disk.capacity_bytes, CAP)
            # référence creuse de même taille
            with open(REF, "wb") as fh:
                fh.truncate(CAP)
                for off, ln, tag in REGIONS:
                    data = pattern(tag, off, ln)
                    disk._write_unsafe(off, data)
                    fh.seek(off); fh.write(data)
                    print(f"  écrit {tag.decode()}  offset={off:>13,}  taille={ln:>11,}")

    print(f"VMDK après écriture : {os.path.getsize(DISK):,} octets sur disque")
    h = hashlib.sha256()
    with open(REF, "rb") as fh:
        for blk in iter(lambda: fh.read(8 << 20), b""):
            h.update(blk)
    print(f"référence : {REF}\nsha256    : {h.hexdigest()}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
