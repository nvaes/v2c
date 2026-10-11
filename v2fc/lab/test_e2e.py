"""
POC L1 — chaîne complète hors vCenter.

VMDK source (transport file) → lecture VDDK → plan d'extents →
agent relais (pwrite aux offsets) → vérification SHA-256.

Couvre : seed, delta, pause/reprise, mise à zéro d'une région,
contrôle de signature, refus d'appariement erroné.
"""
import hashlib, logging, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datamover import vddk, extents as ex
from relay.writer import VolumeWriter, Mode, SignatureMismatch, checksum
from make_disk import pattern, REGIONS, CAP, DISK, REF, DISTRIB

LAB = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(LAB, "target.raw")
VOL_UUID = "fc-volume-0a1b2c3d-4e5f"
CHUNK = 4 << 20

failures = 0
def check(cond, label, detail=""):
    global failures
    print(f"  {'OK   ' if cond else 'ECHEC'} {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond: failures += 1

def sha(path, size=None):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        left = size if size is not None else os.path.getsize(path)
        while left:
            blk = fh.read(min(8 << 20, left))
            if not blk: break
            h.update(blk); left -= len(blk)
    return h.hexdigest()

VOLSIZE = VolumeWriter.required_volume_size(CAP)   # capacité + signature, arrondi 1 Mio

def provision(path, uuid, size=None):
    """Provisionne un volume cible : plus grand que la source d'au moins
    un secteur, puis y dépose la signature (hors zone de données)."""
    with open(path, "wb") as fh:
        fh.truncate(size or VOLSIZE)
    VolumeWriter.stamp_signature(path, uuid)

def transfer(disk, plan, writer, stop_after=None):
    """Copie le plan. `stop_after` simule une interruption -> renvoie le curseur."""
    done = 0
    for chunk in plan:
        if stop_after is not None and done >= stop_after:
            return chunk.idx                      # première tranche NON confirmée
        data = disk.read(chunk.offset, chunk.length)
        writer.write_chunk(chunk.offset, data, expected_crc=checksum(data))
        done += 1
    writer.flush()
    return None

def main():
    global failures
    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")
    vddk.init(DISTRIB, require=["file"])

    # ---------------------------------------------------------------- SEED
    print("\n=== 1. SEED — copie initiale des blocs alloués ===")
    provision(TARGET, VOL_UUID)
    with vddk.Connection.local() as conn, conn.open(DISK) as disk:
        allocated = disk.query_allocated_blocks(1 << 20)
        plan = ex.build_plan(allocated, disk.capacity_bytes, CHUNK)
        print(f"  {len(allocated)} extents -> {len(plan)} tranches, "
              f"{ex.total_bytes(plan):,} octets à copier "
              f"({100*ex.total_bytes(plan)/CAP:.2f} % du disque)")
        t0 = time.time()
        with VolumeWriter(TARGET, CAP, Mode.SEED, VOL_UUID) as w:
            transfer(disk, plan, w)
            st = w.stats
        dt = time.time() - t0
    print(f"  {st.bytes_written:,} octets écrits en {dt:.1f} s "
          f"({st.bytes_written/dt/2**20:.0f} Mio/s), "
          f"{st.chunks_skipped_zero} tranches nulles ignorées, O_DIRECT={st.direct_io}")

    # la signature est hors zone de données : on compare la capacité entière
    n = CAP
    check(sha(TARGET, n) == sha(REF, n), "cible identique à la référence (SHA-256)")
    check(os.stat(TARGET).st_blocks * 512 < 64 << 20,
          f"cible restée creuse ({os.stat(TARGET).st_blocks*512:,} o alloués)")

    # --------------------------------------------------------------- DELTA
    print("\n=== 2. DELTA — modification, mise à zéro, resynchronisation ===")
    changed = [
        (1 << 20,          2 << 20, b"DLT1"),         # modifie dans BOOT
        (512 << 20,        1 << 20, b"DLT2"),         # modifie dans MIDL
        (1024 << 20,       4 << 20, b"NEW1"),         # zone neuve, jamais écrite
    ]
    zeroed = (1544 << 20, 4 << 20)                     # met à zéro dans TAIL

    with vddk.Connection.local(read_only=False) as conn, \
         conn.open(DISK, read_only=False) as disk, open(REF, "r+b") as ref:
        for off, ln, tag in changed:
            data = pattern(tag, off, ln)
            disk._write_unsafe(off, data)
            ref.seek(off); ref.write(data)
        disk._write_unsafe(zeroed[0], bytes(zeroed[1]))
        ref.seek(zeroed[0]); ref.write(bytes(zeroed[1]))

    # ce que CBT renverrait : les zones touchées (volontairement désalignées
    # et en désordre, pour éprouver le planificateur)
    cbt = [{"start": off + 7, "length": ln - 13} for off, ln, _ in changed]
    cbt.append({"start": zeroed[0] + 1, "length": zeroed[1] - 1})
    cbt.reverse()
    delta_extents = ex.extents_from_changed_areas(cbt)

    with vddk.Connection.local() as conn, conn.open(DISK) as disk:
        plan = ex.build_plan(delta_extents, disk.capacity_bytes, CHUNK)
        print(f"  CBT : {len(cbt)} zones désalignées -> {len(plan)} tranches, "
              f"{ex.total_bytes(plan):,} octets ({100*ex.total_bytes(plan)/CAP:.2f} %)")
        with VolumeWriter(TARGET, CAP, Mode.DELTA, VOL_UUID) as w:
            transfer(disk, plan, w)
            st = w.stats
    print(f"  {st.bytes_written:,} octets écrits, {st.chunks_punched} tranches perforées")
    check(sha(TARGET, n) == sha(REF, n), "cible à jour après delta (SHA-256)")
    with open(TARGET, "rb") as fh:
        fh.seek(zeroed[0]); check(fh.read(zeroed[1]) == bytes(zeroed[1]),
                                  "région remise à zéro côté cible")

    # -------------------------------------------------------- PAUSE/REPRISE
    print("\n=== 3. PAUSE / REPRISE au milieu d'un round ===")
    provision(TARGET, VOL_UUID)
    with vddk.Connection.local() as conn, conn.open(DISK) as disk:
        plan = ex.build_plan(disk.query_allocated_blocks(1 << 20),
                             disk.capacity_bytes, CHUNK)
        half = len(plan) // 2
        with VolumeWriter(TARGET, CAP, Mode.SEED, VOL_UUID) as w:
            cursor = transfer(disk, plan, w, stop_after=half)
        print(f"  interrompu après {half}/{len(plan)} tranches, curseur = {cursor}")
        check(sha(TARGET, n) != sha(REF, n), "cible incomplète à la pause")

        rest = ex.resume_from(plan, cursor)
        print(f"  reprise : {len(rest)} tranches restantes")
        with VolumeWriter(TARGET, CAP, Mode.SEED, VOL_UUID) as w:
            transfer(disk, rest, w)
    check(sha(TARGET, n) == sha(REF, n), "cible complète après reprise (SHA-256)")

    # ----------------------------------------------------------- SIGNATURE
    print("\n=== 4. Garde-fou d'appariement de volume (§7.2) ===")
    wrong = os.path.join(LAB, "wrong.raw")
    provision(wrong, "fc-volume-UN-AUTRE-VOLUME")
    try:
        VolumeWriter(wrong, CAP, Mode.SEED, VOL_UUID).close()
        check(False, "mauvais volume refusé", "aucune exception")
    except SignatureMismatch:
        check(True, "mauvais volume refusé (SignatureMismatch)")
    try:
        VolumeWriter(wrong, CAP * 2, Mode.SEED, verify_signature=False).close()
        check(False, "volume trop petit refusé", "aucune exception")
    except Exception as exc:
        check("il en faut au moins" in str(exc), "volume trop petit refusé")
    os.remove(wrong)

    # ------------------------------------------------------------ INTEGRITE
    print("\n=== 5. Détection de corruption en transport ===")
    with VolumeWriter(TARGET, CAP, Mode.DELTA, VOL_UUID) as w:
        good = b"A" * 4096
        try:
            w.write_chunk(0, good, expected_crc=checksum(good) ^ 0xFFFF)
            check(False, "somme de contrôle divergente détectée", "aucune exception")
        except Exception as exc:
            check("contrôle divergente" in str(exc), "somme de contrôle divergente détectée")
        check(w.stats.crc_mismatches == 1, "compteur de divergences incrémenté")

    print(f"\n{'=== POC L1 : TOUS LES CONTROLES PASSENT ===' if not failures else f'=== {failures} ECHEC(S) ==='}")
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
