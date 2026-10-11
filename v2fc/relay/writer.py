"""
Agent relais — écriture des extents dans les volumes cibles FusionCompute.

Tourne dans la VM relais, avec les volumes cibles attachés en SCSI.
Écrit les tranches reçues à leur offset exact (ARCHITECTURE.md §7).

Deux garde-fous structurants :

  * SIGNATURE (§7.2). Avant toute écriture, on relit la signature déposée au
    provisionnement dans le dernier secteur du VOLUME. Si elle ne correspond
    pas, on refuse d'écrire : c'est la protection contre l'appariement
    erroné entre un UUID de volume FusionCompute et un /dev/sdX.

    La signature vit dans le RÉSERVOIR situé au-delà de la capacité du disque
    source, jamais à l'intérieur. Deux raisons, découvertes au banc d'essai :
    une signature placée dans la zone de données est écrasée par la première
    tranche qui couvre la fin du disque — le contrôle devient alors faussement
    négatif au round suivant — et elle corromprait le dernier secteur de
    l'invité migré (table de partition GPT de secours, entre autres).
    Le volume cible doit donc être provisionné avec au moins un secteur de
    plus que la source ; l'arrondi au granule du datastore le fournit
    généralement, sinon on arrondit explicitement.

  * SEED vs DELTA (§7.3). Au seed, le volume est neuf donc déjà nul : une
    tranche nulle n'est pas écrite, ce qui préserve le thin provisioning.
    Au delta, une tranche nulle DOIT effacer ce qui était là : on perfore
    (FALLOC_FL_PUNCH_HOLE) si le support l'accepte, sinon on écrit des zéros.
"""

from __future__ import annotations

import logging
import mmap
import os
import stat
import zlib
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger("v2fc.relay")

SECTOR_SIZE = 512
SIGNATURE_MAGIC = b"V2FC-VOL"          # 8 octets, puis l'UUID du volume

FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02


class Mode(str, Enum):
    SEED = "SEED"
    DELTA = "DELTA"


class WriterError(RuntimeError):
    pass


class SignatureMismatch(WriterError):
    """Le volume ouvert n'est pas celui attendu. Ne jamais passer outre."""


@dataclass
class Stats:
    chunks_written: int = 0
    chunks_skipped_zero: int = 0
    chunks_punched: int = 0
    bytes_written: int = 0
    bytes_skipped: int = 0
    crc_mismatches: int = 0
    direct_io: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def checksum(data: bytes) -> int:
    """
    Somme de contrôle de transport. zlib.crc32 suffit pour détecter une
    corruption sur un lien mTLS ; en production, préférer CRC32C (paquet
    google-crc32c) qui utilise l'instruction SSE4.2 et coûte quasiment rien.
    """
    return zlib.crc32(data) & 0xFFFFFFFF


class VolumeWriter:
    """
    Écrivain pour UN volume cible.

    `path` est un périphérique bloc (/dev/sdX) en production, un fichier
    creux au banc d'essai. À utiliser comme gestionnaire de contexte.
    """

    def __init__(self, path: str, capacity: int, mode: Mode,
                 volume_uuid: str | None = None,
                 verify_signature: bool = True,
                 direct: bool = True,
                 align: int = 4096) -> None:
        """
        `capacity` : capacité du disque SOURCE. C'est la borne au-delà de
        laquelle aucune écriture de données n'est permise. Le volume
        lui-même est plus grand ; la différence porte la signature.
        """
        self.path = path
        self.capacity = capacity
        self.mode = Mode(mode)
        self.volume_uuid = volume_uuid
        self.align = align
        self.stats = Stats()
        self._buf: mmap.mmap | None = None
        self.device_size = 0

        flags = os.O_WRONLY
        if direct and hasattr(os, "O_DIRECT"):
            try:
                self._fd = os.open(path, flags | os.O_DIRECT)
                self.stats.direct_io = True
            except OSError as exc:
                log.warning("O_DIRECT indisponible sur %s (%s) — repli en écriture "
                            "tamponnée avec fdatasync", path, exc.strerror)
                self._fd = os.open(path, flags)
        else:
            self._fd = os.open(path, flags)

        self._check_capacity()
        if verify_signature:
            self._check_signature()

    # -- contrôles d'ouverture -------------------------------------------
    @staticmethod
    def _device_size(fd: int) -> int:
        st = os.fstat(fd)
        if stat.S_ISBLK(st.st_mode):
            size = os.lseek(fd, 0, os.SEEK_END)
            os.lseek(fd, 0, os.SEEK_SET)
            return size
        return st.st_size

    @staticmethod
    def required_volume_size(capacity: int, granule: int = 1 << 20) -> int:
        """
        Taille minimale du volume cible : capacité source + un secteur de
        signature, arrondie au granule du datastore.
        """
        return -(-(capacity + SECTOR_SIZE) // granule) * granule

    def _check_capacity(self) -> None:
        self.device_size = self._device_size(self._fd)
        if self.device_size < self.capacity + SECTOR_SIZE:
            raise WriterError(
                f"{self.path} fait {self.device_size:,} octets ; il en faut au "
                f"moins {self.capacity + SECTOR_SIZE:,} (capacité source "
                f"{self.capacity:,} + 1 secteur de signature). Mauvais volume ou "
                f"provisionnement insuffisant.")

    def _check_signature(self) -> None:
        """Relit la signature du dernier secteur du VOLUME — protection §7.2."""
        if not self.volume_uuid:
            log.warning("aucun UUID de volume fourni : contrôle de signature ignoré")
            return
        expected = self._signature_bytes(self.volume_uuid)
        with open(self.path, "rb") as fh:
            fh.seek(self.device_size - SECTOR_SIZE)
            got = fh.read(SECTOR_SIZE)
        if got != expected:
            raise SignatureMismatch(
                f"signature absente ou différente sur {self.path} : ce périphérique "
                f"n'est pas le volume {self.volume_uuid}. Écriture refusée.")
        log.info("signature vérifiée sur %s (volume %s, %d octets)",
                 self.path, self.volume_uuid, self.device_size)

    @staticmethod
    def _signature_bytes(volume_uuid: str) -> bytes:
        body = SIGNATURE_MAGIC + volume_uuid.encode()[:64].ljust(64, b"\0")
        return body.ljust(SECTOR_SIZE, b"\0")

    @classmethod
    def stamp_signature(cls, path: str, volume_uuid: str) -> None:
        """
        Dépose la signature au provisionnement, avant tout transfert, dans le
        dernier secteur du volume — hors de la zone de données.
        """
        fd = os.open(path, os.O_RDWR)
        try:
            size = cls._device_size(fd)
            os.pwrite(fd, cls._signature_bytes(volume_uuid), size - SECTOR_SIZE)
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def erase_signature(cls, path: str) -> None:
        """
        À appeler avant de détacher le volume du relais pour l'attacher à la
        VM cible (§10 étape 8). La signature est hors de la zone de données,
        donc inoffensive, mais on ne laisse pas de résidu sur le disque livré.
        """
        fd = os.open(path, os.O_RDWR)
        try:
            size = cls._device_size(fd)
            os.pwrite(fd, bytes(SECTOR_SIZE), size - SECTOR_SIZE)
            os.fsync(fd)
        finally:
            os.close(fd)

    # -- écriture --------------------------------------------------------
    def _aligned_buffer(self, size: int) -> mmap.mmap:
        """mmap anonyme : mémoire alignée page, requise par O_DIRECT."""
        if self._buf is None or len(self._buf) < size:
            if self._buf is not None:
                self._buf.close()
            self._buf = mmap.mmap(-1, max(size, 1 << 20))
        return self._buf

    def write_chunk(self, offset: int, data: bytes,
                    expected_crc: int | None = None) -> None:
        if expected_crc is not None:
            actual = checksum(data)
            if actual != expected_crc:
                self.stats.crc_mismatches += 1
                raise WriterError(
                    f"somme de contrôle divergente à l'offset {offset:,} : "
                    f"attendu {expected_crc:#010x}, calculé {actual:#010x}")

        if offset % SECTOR_SIZE or len(data) % SECTOR_SIZE:
            raise WriterError(f"offset={offset} / taille={len(data)} non alignés")
        if offset + len(data) > self.capacity:
            raise WriterError(f"écriture hors volume : {offset}+{len(data)} > "
                              f"{self.capacity}")

        if data.count(0) == len(data):
            self._handle_zero(offset, len(data))
            return

        self._pwrite(offset, data)
        self.stats.chunks_written += 1
        self.stats.bytes_written += len(data)

    def _handle_zero(self, offset: int, length: int) -> None:
        if self.mode is Mode.SEED:
            # volume neuf : déjà nul, ne rien écrire préserve le thin
            self.stats.chunks_skipped_zero += 1
            self.stats.bytes_skipped += length
            return
        # delta : la zone doit devenir nulle
        try:
            os.posix_fallocate  # noqa: B018
            self._punch_hole(offset, length)
            self.stats.chunks_punched += 1
            self.stats.bytes_written += length
        except (AttributeError, OSError):
            self._pwrite(offset, bytes(length))
            self.stats.chunks_written += 1
            self.stats.bytes_written += length

    def _punch_hole(self, offset: int, length: int) -> None:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int64, ctypes.c_int64]
        rc = libc.fallocate(self._fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
                            offset, length)
        if rc != 0:
            raise OSError(ctypes.get_errno(), "fallocate(PUNCH_HOLE)")

    def _pwrite(self, offset: int, data: bytes) -> None:
        if self.stats.direct_io:
            buf = self._aligned_buffer(len(data))
            buf[:len(data)] = data
            view = memoryview(buf)[:len(data)]
            written = os.pwrite(self._fd, view, offset)
        else:
            written = os.pwrite(self._fd, data, offset)
        if written != len(data):
            raise WriterError(f"écriture partielle à {offset:,} : "
                              f"{written} sur {len(data)} octets")

    # -- clôture ---------------------------------------------------------
    def flush(self) -> None:
        """
        À appeler à la fin de chaque round, AVANT de persister le changeId.
        L'ordre inverse créerait un trou silencieux (ARCHITECTURE.md §8.2).
        """
        os.fdatasync(self._fd)

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.fdatasync(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None
        if self._buf is not None:
            self._buf.close()
            self._buf = None

    def __enter__(self) -> "VolumeWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
