"""
Liaison ctypes du VMware Virtual Disk Development Kit (VDDK 8.x).

Implémente l'interface SourceReader du Data Mover V2FC : ouverture d'un
disque en LECTURE SEULE sur une référence de snapshot, interrogation des
blocs alloués, et lecture par extents.

Prérequis d'exécution — voir ARCHITECTURE.md §2.2 :
  * hôte Linux x86-64, glibc >= 2.16 ;
  * LD_LIBRARY_PATH doit contenir <distrib>/lib64 AVANT le démarrage du
    processus, sinon le plugin de transport avancé (san, hotadd) ne se
    charge pas et InitEx renvoie quand même VIX_OK ;
  * le chemin passé à init() est la RACINE de la distribution.

Invariant I2 (ARCHITECTURE.md §4) : ce module n'expose aucune écriture
vers un disque source. VixDiskLib_Write n'est lié que pour le banc
d'essai, sous un nom explicite et refusé si le disque a été ouvert en
lecture seule.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import (POINTER, Structure, Union, byref, c_char, c_char_p, c_int,
                    c_long, c_size_t, c_uint8, c_uint32, c_uint64, c_void_p)
from dataclasses import dataclass
from typing import Iterator

log = logging.getLogger("v2fc.vddk")

# --------------------------------------------------------------------------
# Constantes issues de vixDiskLib.h
# --------------------------------------------------------------------------
VIX_OK = 0
SECTOR_SIZE = 512

FLAG_OPEN_UNBUFFERED = 1 << 0
FLAG_OPEN_SINGLE_LINK = 1 << 1
FLAG_OPEN_READ_ONLY = 1 << 2

MIN_CHUNK_SIZE = 64 * 2          # 128 secteurs = 64 Kio
MAX_CHUNK_NUMBER = 512 * 1024

CRED_UID = 1
CRED_SESSIONID = 2
CRED_TICKETID = 3
CRED_SSPI = 4

SPEC_VMX = 0
SPEC_VSTORAGE_OBJECT = 1
SPEC_DATASTORE = 2

VixError = c_uint64
SectorType = c_uint64


def error_code(err: int) -> int:
    """VIX_ERROR_CODE — les 16 bits de poids faible portent le code."""
    return err & 0xFFFF


class VddkError(RuntimeError):
    def __init__(self, err: int, text: str, where: str = "") -> None:
        self.err = err
        self.code = error_code(err)
        self.text = text
        prefix = f"{where}: " if where else ""
        super().__init__(f"{prefix}{text} (VixError=0x{err:x}, code={self.code})")


# --------------------------------------------------------------------------
# Structures — alignement natif : le pshpack4 de l'en-tête est _WIN32 only
# --------------------------------------------------------------------------
class Geometry(Structure):
    _fields_ = [("cylinders", c_uint32), ("heads", c_uint32), ("sectors", c_uint32)]


class DiskInfo(Structure):
    _fields_ = [
        ("biosGeo", Geometry),
        ("physGeo", Geometry),
        ("capacity", SectorType),
        ("adapterType", c_int),
        ("numLinks", c_int),
        ("parentFileNameHint", c_char_p),
        ("uuid", c_char_p),
        ("logicalSectorSize", c_uint32),
        ("physicalSectorSize", c_uint32),
    ]


class Block(Structure):
    _fields_ = [("offset", SectorType), ("length", SectorType)]


class BlockList(Structure):
    _fields_ = [("numBlocks", c_uint32), ("blocks", Block * 1)]


class _UidCreds(Structure):
    _fields_ = [("userName", c_char_p), ("password", c_char_p)]


class _SessionCreds(Structure):
    _fields_ = [("cookie", c_char_p), ("userName", c_char_p), ("key", c_char_p)]


class _Creds(Union):
    _fields_ = [("uid", _UidCreds), ("sessionId", _SessionCreds), ("ticketId", c_void_p)]


class _VStorageObjectSpec(Structure):
    _fields_ = [("id", c_char_p), ("datastoreMoRef", c_char_p), ("ssId", c_char_p)]


class _DatastoreSpec(Structure):
    _fields_ = [("datastoreMoRef", c_char_p), ("diskFolder", c_char_p)]


class _Spec(Union):
    _fields_ = [("vStorageObjSpec", _VStorageObjectSpec), ("dsSpec", _DatastoreSpec)]


class ConnectParams(Structure):
    _fields_ = [
        ("vmxSpec", c_char_p),
        ("serverName", c_char_p),
        ("thumbPrint", c_char_p),
        ("privateUse", c_long),
        ("credType", c_int),
        ("creds", _Creds),
        ("port", c_uint32),
        ("nfcHostPort", c_uint32),
        ("vimApiVer", c_char_p),
        ("reserved", c_char * 8),
        ("state", c_void_p),
        ("spec", _Spec),
        ("specType", c_int),
    ]


LogFunc = ctypes.CFUNCTYPE(None, c_char_p, c_void_p)


@dataclass(frozen=True)
class Extent:
    """Une plage d'octets. Toujours alignée sur le secteur."""
    offset: int
    length: int

    @property
    def end(self) -> int:
        return self.offset + self.length

    def __repr__(self) -> str:
        return f"Extent(off={self.offset:,}, len={self.length:,})"


# --------------------------------------------------------------------------
# Bibliothèque — InitEx est global au processus
# --------------------------------------------------------------------------
class _Library:
    _lock = threading.Lock()
    _instance: "_Library | None" = None
    JOURNAL_MAX = 400

    def __init__(self, distrib: str) -> None:
        self.journal: list[str] = []
        self.distrib = os.path.abspath(distrib.rstrip("/"))
        if os.path.basename(self.distrib) == "lib64":
            raise ValueError(
                f"{self.distrib} est le sous-répertoire lib64. VDDK y concatène "
                f"« lib64/ » lui-même : passer {os.path.dirname(self.distrib)}"
            )
        self.lib64 = os.path.join(self.distrib, "lib64")
        self._check_ld_library_path()

        self._libc = ctypes.CDLL("libc.so.6")
        self._libc.vsnprintf.restype = c_int
        self._libc.vsnprintf.argtypes = [c_char_p, c_size_t, c_char_p, c_void_p]

        self.lib = ctypes.CDLL(os.path.join(self.lib64, "libvixDiskLib.so.8"),
                               mode=ctypes.RTLD_GLOBAL)
        self._declare()
        self._callbacks: list = []
        self._init()

    def _check_ld_library_path(self) -> None:
        paths = [os.path.abspath(p) for p in
                 os.environ.get("LD_LIBRARY_PATH", "").split(":") if p]
        if self.lib64 not in paths:
            raise EnvironmentError(
                f"LD_LIBRARY_PATH ne contient pas {self.lib64}. Le VDDK ne pose "
                f"aucun RPATH : sans cela le plugin de transport avancé (san, "
                f"hotadd) ne se charge pas, et InitEx renvoie quand même VIX_OK. "
                f"Poser la variable avant de démarrer le processus (systemd "
                f"Environment=, Dockerfile ENV) — la modifier depuis os.environ "
                f"est sans effet."
            )

    def _declare(self) -> None:
        f = self.lib
        f.VixDiskLib_InitEx.restype = VixError
        f.VixDiskLib_InitEx.argtypes = [c_uint32, c_uint32, LogFunc, LogFunc,
                                        LogFunc, c_char_p, c_char_p]
        f.VixDiskLib_Exit.restype = None
        # ATTENTION — GetErrorText renvoie un tampon que l'appelant DOIT
        # libérer par FreeErrorText. Il faut donc déclarer restype c_void_p,
        # PAS c_char_p : avec c_char_p, ctypes convertit le retour en objet
        # bytes Python et le pointeur d'origine est perdu. Le rendre à
        # FreeErrorText revient alors à libérer de la mémoire appartenant à
        # Python — glibc avorte le processus sur
        # « munmap_chunk(): invalid pointer », et le message d'erreur réel
        # disparaît avec lui. Diagnostiqué sur plateforme : le chemin de
        # remontée d'erreur tuait le processus avant de rapporter l'erreur.
        f.VixDiskLib_GetErrorText.restype = c_void_p
        f.VixDiskLib_GetErrorText.argtypes = [VixError, c_char_p]
        f.VixDiskLib_FreeErrorText.argtypes = [c_void_p]
        f.VixDiskLib_ListTransportModes.restype = c_char_p

        f.VixDiskLib_AllocateConnectParams.restype = POINTER(ConnectParams)
        f.VixDiskLib_FreeConnectParams.argtypes = [POINTER(ConnectParams)]

        f.VixDiskLib_Connect.restype = VixError
        f.VixDiskLib_Connect.argtypes = [POINTER(ConnectParams), POINTER(c_void_p)]
        f.VixDiskLib_ConnectEx.restype = VixError
        f.VixDiskLib_ConnectEx.argtypes = [POINTER(ConnectParams), c_int, c_char_p,
                                           c_char_p, POINTER(c_void_p)]
        f.VixDiskLib_Disconnect.restype = VixError
        f.VixDiskLib_Disconnect.argtypes = [c_void_p]
        f.VixDiskLib_Cleanup.restype = VixError
        f.VixDiskLib_Cleanup.argtypes = [POINTER(ConnectParams),
                                         POINTER(c_uint32), POINTER(c_uint32)]

        f.VixDiskLib_PrepareForAccess.restype = VixError
        f.VixDiskLib_PrepareForAccess.argtypes = [POINTER(ConnectParams), c_char_p]
        f.VixDiskLib_EndAccess.restype = VixError
        f.VixDiskLib_EndAccess.argtypes = [POINTER(ConnectParams), c_char_p]

        f.VixDiskLib_Open.restype = VixError
        f.VixDiskLib_Open.argtypes = [c_void_p, c_char_p, c_uint32, POINTER(c_void_p)]
        f.VixDiskLib_Close.restype = VixError
        f.VixDiskLib_Close.argtypes = [c_void_p]

        f.VixDiskLib_GetInfo.restype = VixError
        f.VixDiskLib_GetInfo.argtypes = [c_void_p, POINTER(POINTER(DiskInfo))]
        f.VixDiskLib_FreeInfo.argtypes = [POINTER(DiskInfo)]
        f.VixDiskLib_GetTransportMode.restype = c_char_p
        f.VixDiskLib_GetTransportMode.argtypes = [c_void_p]

        f.VixDiskLib_Read.restype = VixError
        f.VixDiskLib_Read.argtypes = [c_void_p, SectorType, SectorType, POINTER(c_uint8)]
        f.VixDiskLib_Write.restype = VixError
        f.VixDiskLib_Write.argtypes = [c_void_p, SectorType, SectorType, POINTER(c_uint8)]

        f.VixDiskLib_QueryAllocatedBlocks.restype = VixError
        f.VixDiskLib_QueryAllocatedBlocks.argtypes = [
            c_void_p, SectorType, SectorType, SectorType, POINTER(POINTER(BlockList))]
        f.VixDiskLib_FreeBlockList.restype = VixError
        f.VixDiskLib_FreeBlockList.argtypes = [POINTER(BlockList)]

    def _init(self) -> None:
        def make(level: int, tag: str):
            def cb(fmt, ap):
                buf = ctypes.create_string_buffer(4096)
                self._libc.vsnprintf(buf, 4096, fmt, ap)
                text = buf.value.decode(errors="replace").strip()
                log.log(level, "vddk: %s", text)
                # tampon circulaire : les VixError sont souvent génériques
                # (VIX_E_FAIL), et la vraie cause n'existe que dans ce
                # journal. On le conserve même quand le niveau de trace de
                # l'application le masque.
                self.journal.append(f"{tag} {text}")
                if len(self.journal) > self.JOURNAL_MAX:
                    del self.journal[:len(self.journal) - self.JOURNAL_MAX]
            wrapped = LogFunc(cb)
            self._callbacks.append(wrapped)   # le GC libérerait le trampoline
            return wrapped

        err = self.lib.VixDiskLib_InitEx(
            8, 0, make(logging.DEBUG, "[log ]"), make(logging.WARNING, "[warn]"),
            make(logging.ERROR, "[!!!!]"), self.distrib.encode(), None)
        self.check(err, "VixDiskLib_InitEx")
        log.info("VDDK initialisé, transports disponibles : %s", self.transport_modes)

    def check(self, err: int, where: str) -> None:
        """
        Convertit une VixError en exception Python.

        Le pointeur renvoyé par GetErrorText est conservé tel quel : on lit
        son contenu avec string_at, puis on rend EXACTEMENT ce pointeur à
        FreeErrorText. Voir le commentaire dans _declare() — toute variante
        qui laisse ctypes copier la chaîne fait avorter le processus.
        """
        if err == VIX_OK:
            return
        ptr = self.lib.VixDiskLib_GetErrorText(err, None)
        if ptr:
            try:
                text = ctypes.string_at(ptr).decode(errors="replace")
            finally:
                self.lib.VixDiskLib_FreeErrorText(c_void_p(ptr))
        else:
            text = "erreur inconnue"
        raise VddkError(err, text, where)

    def journal_mark(self) -> int:
        """Position courante du journal, à passer à journal_since()."""
        return len(self.journal)

    def journal_since(self, mark: int, limit: int = 40) -> list[str]:
        """
        Messages VDDK émis depuis `mark`. C'est là que se trouve la cause
        réelle d'un VIX_E_FAIL, que le code d'erreur ne dit pas.
        """
        return self.journal[mark:][-limit:]

    @property
    def transport_modes(self) -> list[str]:
        raw = self.lib.VixDiskLib_ListTransportModes()
        return raw.decode().split(":") if raw else []

    def shutdown(self) -> None:
        self.lib.VixDiskLib_Exit()


def init(distrib: str, require: list[str] | None = None) -> _Library:
    """
    Initialise le VDDK (idempotent, global au processus).

    `require` : modes de transport exigés. Le démarrage échoue s'ils ne sont
    pas disponibles, plutôt que de laisser la migration se dérouler en mode
    dégradé sans alerte (ARCHITECTURE.md §2.2, règle 2).
    """
    with _Library._lock:
        if _Library._instance is None:
            _Library._instance = _Library(distrib)
        inst = _Library._instance
    if require:
        available = inst.transport_modes
        missing = [m for m in require if m not in available]
        if missing:
            raise EnvironmentError(
                f"modes de transport exigés indisponibles : {', '.join(missing)} "
                f"(disponibles : {':'.join(available)})")
    return inst


def _lib() -> _Library:
    if _Library._instance is None:
        raise RuntimeError("vddk.init(distrib) n'a pas été appelé")
    return _Library._instance


def journal_mark() -> int:
    return _lib().journal_mark()


def journal_since(mark: int, limit: int = 40) -> list[str]:
    return _lib().journal_since(mark, limit)


# --------------------------------------------------------------------------
# Connexion
# --------------------------------------------------------------------------
class Connection:
    """
    Connexion VDDK. Deux usages :

      * locale (banc d'essai, transport « file ») : Connection.local()
      * vSphere : Connection.vsphere(server, user, password, thumbprint,
                  vmx_spec, snapshot_ref=...)  ← toujours en lecture seule

    À utiliser comme gestionnaire de contexte.
    """

    def __init__(self, handle: c_void_p, params, read_only: bool,
                 transport_modes: str | None) -> None:
        self._h = handle
        self._params = params
        self.read_only = read_only
        self.requested_modes = transport_modes

    # -- fabriques -------------------------------------------------------
    @classmethod
    def local(cls, read_only: bool = True) -> "Connection":
        lib = _lib()
        conn = c_void_p()
        lib.check(lib.lib.VixDiskLib_ConnectEx(None, 1 if read_only else 0,
                                               None, b"file", byref(conn)),
                  "VixDiskLib_ConnectEx(local)")
        return cls(conn, None, read_only, "file")

    @classmethod
    def vsphere(cls, server: str, user: str, password: str, thumbprint: str,
                vmx_spec: str, snapshot_ref: str | None = None,
                transport_modes: str = "hotadd:nbdssl:nbd",
                port: int = 443) -> "Connection":
        """
        Connexion à vCenter/ESXi. Forcée en lecture seule : invariant I2.

        thumbprint : empreinte SHA-1 du certificat, au format
                     « AA:BB:CC:… » (obligatoire, pas de contournement TLS).
        vmx_spec   : « MaVM/MaVM.vmx?dcPath=Chemin/Datacenter&dsName=datastore1 »
        snapshot_ref : moref du snapshot de migration ; sans lui, la lecture
                     porterait sur l'état courant d'une VM allumée, donc
                     incohérente.
        """
        lib = _lib()
        params = lib.lib.VixDiskLib_AllocateConnectParams()
        if not params:
            raise RuntimeError("VixDiskLib_AllocateConnectParams a renvoyé NULL")
        p = params.contents
        # les bytes doivent survivre à l'appel : on les garde côté Python
        keep = [vmx_spec.encode(), server.encode(), thumbprint.encode(),
                user.encode(), password.encode()]
        p.vmxSpec, p.serverName, p.thumbPrint = keep[0], keep[1], keep[2]
        p.credType = CRED_UID
        p.creds.uid.userName, p.creds.uid.password = keep[3], keep[4]
        p.port = port
        p.specType = SPEC_VMX

        conn = c_void_p()
        err = lib.lib.VixDiskLib_ConnectEx(
            params, 1, snapshot_ref.encode() if snapshot_ref else None,
            transport_modes.encode(), byref(conn))
        if err != VIX_OK:
            lib.lib.VixDiskLib_FreeConnectParams(params)
            lib.check(err, "VixDiskLib_ConnectEx(vsphere)")
        obj = cls(conn, params, True, transport_modes)
        obj._keep = keep          # noqa: SLF001 — empêche le GC des buffers
        return obj

    @classmethod
    def vsphere_auto(cls, host: str, user: str, password: str, thumbprint: str,
                     vmx_spec_candidates: list[tuple[str, str]],
                     snapshot_ref: str | None = None,
                     transport_modes: str = "hotadd:nbdssl:nbd",
                     port: int = 443, probe=None) -> tuple[str, "Connection"]:
        """
        Essaie plusieurs formes de vmxSpec, dans l'ordre donné par
        l'appelant, et renvoie (étiquette acceptée, connexion) pour la
        première qui convient.

        journal §5 quater/L1 (cycle réel du 15/09/2026) : `VixDiskLib_ConnectEx`
        réussit pour N'IMPORTE QUELLE forme de vmxSpec testée jusqu'ici — le
        rejet (« Invalid search specifier ») n'apparaît qu'à
        `VixDiskLib_Open()`. Se fier à la seule réussite de la connexion pour
        choisir une forme est donc trompeur : `probe`, si fourni, est appelé
        avec la connexion établie et doit lever une exception si la forme ne
        convient pas en pratique (typiquement : ouvrir puis refermer un
        disque réel). Sans `probe`, seule la connexion elle-même est
        éprouvée. Lève la dernière exception si aucune forme ne passe.
        """
        last_exc: Exception | None = None
        for label, vmx_spec in vmx_spec_candidates:
            try:
                conn = cls.vsphere(host, user, password, thumbprint, vmx_spec,
                                   snapshot_ref=snapshot_ref,
                                   transport_modes=transport_modes, port=port)
            except Exception as exc:                   # noqa: BLE001
                last_exc = exc
                log.warning("connexion VDDK refusée avec vmxSpec %r (%s) : %s",
                           vmx_spec, label, exc)
                continue
            if probe is not None:
                try:
                    probe(conn)
                except Exception as exc:                # noqa: BLE001
                    last_exc = exc
                    log.warning("vmxSpec %r (%s) connecté mais rejeté à "
                               "l'ouverture d'un disque : %s", vmx_spec, label, exc)
                    try:
                        conn.close()
                    except Exception:                   # noqa: BLE001
                        log.exception("fermeture de la connexion VDDK refusée")
                    continue
            return label, conn
        assert last_exc is not None
        raise last_exc

    # -- accès HotAdd ----------------------------------------------------
    def prepare_access(self, identity: str = "v2fc") -> None:
        """
        Désactive temporairement vMotion et DRS sur la VM source pendant que
        ses disques sont attachés au proxy (mode HotAdd).

        Doit être appelée AVANT l'ouverture, et impérativement suivie de
        end_access() dans un `finally` : un EndAccess oublié laisse la VM
        source bloquée pour DRS — une mutation source involontaire, donc une
        violation de l'invariant I3.
        """
        if self._params is None:
            return                      # connexion locale : sans objet
        lib = _lib()
        lib.check(lib.lib.VixDiskLib_PrepareForAccess(self._params,
                                                      identity.encode()),
                  "VixDiskLib_PrepareForAccess")
        self._access_identity = identity

    def end_access(self, identity: str | None = None) -> None:
        if self._params is None:
            return
        ident = identity or getattr(self, "_access_identity", None)
        if ident is None:
            return
        lib = _lib()
        err = lib.lib.VixDiskLib_EndAccess(self._params, ident.encode())
        if err != VIX_OK:
            # ne jamais masquer l'exception d'origine, mais tracer haut :
            # la VM source reste bloquée pour DRS jusqu'à intervention
            log.error("VixDiskLib_EndAccess a échoué (0x%x) : la VM source peut "
                      "rester bloquée pour vMotion/DRS", err)
        self._access_identity = None

    # -- ouverture de disque --------------------------------------------
    def open(self, path: str, read_only: bool | None = None,
             single_link: bool = False, unbuffered: bool = False) -> "Disk":
        ro = self.read_only if read_only is None else read_only
        flags = 0
        if ro:
            flags |= FLAG_OPEN_READ_ONLY
        if single_link:
            flags |= FLAG_OPEN_SINGLE_LINK
        if unbuffered:
            flags |= FLAG_OPEN_UNBUFFERED
        lib = _lib()
        h = c_void_p()
        lib.check(lib.lib.VixDiskLib_Open(self._h, path.encode(), flags, byref(h)),
                  f"VixDiskLib_Open({path})")
        return Disk(h, path, ro)

    def close(self) -> None:
        lib = _lib()
        try:
            self.end_access()
        finally:
            if self._h:
                lib.lib.VixDiskLib_Disconnect(self._h)
                self._h = None
            if self._params:
                lib.lib.VixDiskLib_FreeConnectParams(self._params)
                self._params = None

    @staticmethod
    def cleanup(server: str, user: str, password: str, thumbprint: str,
                port: int = 443) -> tuple[int, int]:
        """
        Purge les états laissés par un arrêt brutal (disques restés attachés
        au proxy en HotAdd). À appeler au démarrage de l'orchestrateur.
        Renvoie (nettoyées, restantes).
        """
        lib = _lib()
        params = lib.lib.VixDiskLib_AllocateConnectParams()
        p = params.contents
        keep = [server.encode(), thumbprint.encode(), user.encode(),
                password.encode()]
        p.serverName, p.thumbPrint = keep[0], keep[1]
        p.credType = CRED_UID
        p.creds.uid.userName, p.creds.uid.password = keep[2], keep[3]
        p.port = port
        p.vmxSpec = None                 # obligatoire : None pour Cleanup
        cleaned, remaining = c_uint32(), c_uint32()
        try:
            lib.check(lib.lib.VixDiskLib_Cleanup(params, byref(cleaned),
                                                 byref(remaining)),
                      "VixDiskLib_Cleanup")
        finally:
            lib.lib.VixDiskLib_FreeConnectParams(params)
        if remaining.value:
            log.warning("VixDiskLib_Cleanup : %d VM restent à nettoyer",
                        remaining.value)
        return cleaned.value, remaining.value

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------
# Disque
# --------------------------------------------------------------------------
class Disk:
    def __init__(self, handle: c_void_p, path: str, read_only: bool) -> None:
        self._h = handle
        self.path = path
        self.read_only = read_only
        self._info = self._read_info()
        mode = self.transport_mode
        log.info("disque %s ouvert (%s) — %s, %.1f Gio, secteur %d o",
                 path, "lecture seule" if read_only else "ECRITURE", mode,
                 self.capacity_bytes / 2**30, self.logical_sector_size)

    def _read_info(self) -> dict:
        lib = _lib()
        info = POINTER(DiskInfo)()
        lib.check(lib.lib.VixDiskLib_GetInfo(self._h, byref(info)),
                  "VixDiskLib_GetInfo")
        i = info.contents
        data = {
            "capacity_sectors": i.capacity,
            "adapter_type": i.adapterType,
            "num_links": i.numLinks,
            "logical_sector_size": i.logicalSectorSize or SECTOR_SIZE,
            "physical_sector_size": i.physicalSectorSize or SECTOR_SIZE,
            "uuid": i.uuid.decode() if i.uuid else None,
            "parent": i.parentFileNameHint.decode() if i.parentFileNameHint else None,
            "bios_geo": (i.biosGeo.cylinders, i.biosGeo.heads, i.biosGeo.sectors),
        }
        lib.lib.VixDiskLib_FreeInfo(info)
        return data

    # -- propriétés ------------------------------------------------------
    @property
    def capacity_sectors(self) -> int:
        return self._info["capacity_sectors"]

    @property
    def capacity_bytes(self) -> int:
        # VixDiskLib_Read raisonne toujours en secteurs de 512 o, même si le
        # disque déclare un secteur logique de 4 Kio.
        return self._info["capacity_sectors"] * SECTOR_SIZE

    @property
    def logical_sector_size(self) -> int:
        return self._info["logical_sector_size"]

    @property
    def uuid(self) -> str | None:
        return self._info["uuid"]

    @property
    def info(self) -> dict:
        return dict(self._info)

    @property
    def transport_mode(self) -> str:
        raw = _lib().lib.VixDiskLib_GetTransportMode(self._h)
        return raw.decode() if raw else "?"

    # -- lecture ---------------------------------------------------------
    def read(self, offset: int, length: int) -> bytes:
        """Lit `length` octets à `offset`. Les deux doivent être alignés secteur."""
        if offset % SECTOR_SIZE or length % SECTOR_SIZE:
            raise ValueError(f"offset={offset} et length={length} doivent être "
                             f"multiples de {SECTOR_SIZE}")
        if offset + length > self.capacity_bytes:
            raise ValueError(f"lecture hors capacité : {offset}+{length} > "
                             f"{self.capacity_bytes}")
        buf = ctypes.create_string_buffer(length)
        lib = _lib()
        lib.check(lib.lib.VixDiskLib_Read(
            self._h, offset // SECTOR_SIZE, length // SECTOR_SIZE,
            ctypes.cast(buf, POINTER(c_uint8))), f"VixDiskLib_Read(@{offset})")
        return buf.raw

    def read_into(self, offset: int, buf: ctypes.Array) -> int:
        """Variante sans allocation : relit dans un tampon réutilisé."""
        length = len(buf)
        if offset % SECTOR_SIZE or length % SECTOR_SIZE:
            raise ValueError("offset et taille de tampon doivent être alignés secteur")
        lib = _lib()
        lib.check(lib.lib.VixDiskLib_Read(
            self._h, offset // SECTOR_SIZE, length // SECTOR_SIZE,
            ctypes.cast(buf, POINTER(c_uint8))), f"VixDiskLib_Read(@{offset})")
        return length

    def _write_unsafe(self, offset: int, data: bytes) -> None:
        """
        RÉSERVÉ AU BANC D'ESSAI. Jamais appelé sur un disque source :
        l'invariant I2 interdit toute écriture côté VMware.
        """
        if self.read_only:
            raise PermissionError("disque ouvert en lecture seule (invariant I2)")
        if offset % SECTOR_SIZE or len(data) % SECTOR_SIZE:
            raise ValueError("offset et taille doivent être alignés secteur")
        buf = ctypes.create_string_buffer(data, len(data))
        lib = _lib()
        lib.check(lib.lib.VixDiskLib_Write(
            self._h, offset // SECTOR_SIZE, len(data) // SECTOR_SIZE,
            ctypes.cast(buf, POINTER(c_uint8))), f"VixDiskLib_Write(@{offset})")

    # -- blocs alloués ---------------------------------------------------
    def query_allocated_blocks(self, chunk_bytes: int = 1 << 20) -> list[Extent]:
        """
        Blocs alloués du disque, utilisés pour le seed « used-blocks only ».

        VDDK impose : chunk multiple de 64 Kio, et au plus 512 Ki chunks par
        appel — d'où le fenêtrage. Renvoie des extents en octets, fusionnés.
        """
        lib = _lib()
        chunk_sectors = chunk_bytes // SECTOR_SIZE
        if chunk_sectors % MIN_CHUNK_SIZE:
            raise ValueError(f"chunk_bytes doit être multiple de "
                             f"{MIN_CHUNK_SIZE * SECTOR_SIZE} octets")
        total = self.capacity_sectors
        # la fenêtre doit être un multiple entier de chunk
        window = min(MAX_CHUNK_NUMBER, 1 << 20) * chunk_sectors

        out: list[Extent] = []
        start = 0
        while start < total:
            n = min(window, total - start)
            n -= n % chunk_sectors          # QueryAllocatedBlocks exige un multiple
            if n == 0:
                break
            blocks = POINTER(BlockList)()
            lib.check(lib.lib.VixDiskLib_QueryAllocatedBlocks(
                self._h, start, n, chunk_sectors, byref(blocks)),
                f"VixDiskLib_QueryAllocatedBlocks(@{start})")
            bl = blocks.contents
            count = bl.numBlocks
            if count:
                arr = ctypes.cast(ctypes.byref(bl, BlockList.blocks.offset),
                                  POINTER(Block * count)).contents
                for b in arr:
                    out.append(Extent(b.offset * SECTOR_SIZE, b.length * SECTOR_SIZE))
            lib.lib.VixDiskLib_FreeBlockList(blocks)
            start += n

        return merge_extents(out)

    # -- cycle de vie ----------------------------------------------------
    def close(self) -> None:
        if self._h:
            _lib().lib.VixDiskLib_Close(self._h)
            self._h = None

    def __enter__(self) -> "Disk":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def merge_extents(extents: list[Extent]) -> list[Extent]:
    """Trie et fusionne les extents contigus ou chevauchants."""
    if not extents:
        return []
    ordered = sorted(extents, key=lambda e: e.offset)
    out = [ordered[0]]
    for e in ordered[1:]:
        last = out[-1]
        if e.offset <= last.end:
            if e.end > last.end:
                out[-1] = Extent(last.offset, e.end - last.offset)
        else:
            out.append(e)
    return out
