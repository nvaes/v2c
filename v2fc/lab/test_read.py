"""Valide la lecture VDDK : blocs alloués + relecture bit-exacte."""
import hashlib, logging, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datamover import vddk

DISTRIB = os.environ["VDDK_DISTRIB"]
LAB = os.path.dirname(os.path.abspath(__file__))
DISK, REF = os.path.join(LAB, "test.vmdk"), os.path.join(LAB, "reference.raw")

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    vddk.init(DISTRIB, require=["file"])
    failures = 0
    with vddk.Connection.local() as conn, conn.open(DISK) as disk:
        print(f"\ninfo disque : {disk.info}")
        print(f"transport   : {disk.transport_mode}")

        print("\n--- QueryAllocatedBlocks (chunk 1 Mio) ---")
        allocated = disk.query_allocated_blocks(chunk_bytes=1 << 20)
        total = sum(e.length for e in allocated)
        for e in allocated:
            print(f"  {e}")
        print(f"  {len(allocated)} extents, {total:,} octets alloués "
              f"sur {disk.capacity_bytes:,} ({100*total/disk.capacity_bytes:.2f} %)")

        # les 4 régions écrites doivent être couvertes
        expected = [(0, 4<<20), (512<<20, 8<<20), (1536<<20, 16<<20),
                    (disk.capacity_bytes - (1<<20), 1<<20)]
        for off, ln in expected:
            covered = any(e.offset <= off and e.end >= off + ln for e in allocated)
            print(f"  {'OK   ' if covered else 'ECHEC'} région @{off:,} ({ln:,} o) couverte")
            failures += 0 if covered else 1

        print("\n--- relecture bit-exacte des extents alloués ---")
        with open(REF, "rb") as ref:
            read_bytes = 0
            for e in allocated:
                pos, remaining = e.offset, e.length
                while remaining:
                    n = min(4 << 20, remaining)
                    got = disk.read(pos, n)
                    ref.seek(pos); want = ref.read(n)
                    if got != want:
                        bad = next(i for i in range(n) if got[i] != want[i])
                        print(f"  ECHEC divergence à l'octet {pos + bad:,}")
                        failures += 1
                        remaining = 0
                        break
                    pos += n; remaining -= n; read_bytes += n
            print(f"  {read_bytes:,} octets relus, identiques à la référence")

        print("\n--- lecture d'une zone creuse (doit être nulle) ---")
        hole = disk.read(1 << 30, 1 << 20)
        is_zero = hole == bytes(1 << 20)
        print(f"  {'OK   ' if is_zero else 'ECHEC'} 1 Mio à 1 Gio = zéros")
        failures += 0 if is_zero else 1

        print("\n--- garde-fous ---")
        for label, fn in [
            ("lecture non alignée refusée", lambda: disk.read(123, 4096)),
            ("lecture hors capacité refusée", lambda: disk.read(disk.capacity_bytes, 512)),
            ("écriture refusée en lecture seule", lambda: disk._write_unsafe(0, b"\0"*512)),
        ]:
            try:
                fn(); print(f"  ECHEC {label} — aucune exception"); failures += 1
            except (ValueError, PermissionError) as exc:
                print(f"  OK    {label} ({type(exc).__name__})")

    print(f"\n{'TOUS LES CONTROLES PASSENT' if not failures else f'{failures} ECHEC(S)'}")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
