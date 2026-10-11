#!/usr/bin/env python3
"""
vddk_probe.py — validation de l'environnement VDDK pour le Data Mover V2FC.

Vérifie qu'un hôte Linux x86-64 peut charger et exploiter le VDDK, et
surtout que les modes de transport avancés (san, hotadd) sont bien
disponibles. Voir ARCHITECTURE.md §2.1 et §2.2.

Usage :
    python3 vddk_probe.py /chemin/vers/vmware-vix-disklib-distrib
    python3 vddk_probe.py /chemin/... --require hotadd,nbdssl
    python3 vddk_probe.py /chemin/... -v          # journal VDDK complet

Deux conditions INDÉPENDANTES conditionnent la disponibilité des modes
« san » et « hotadd ». Chacune, si elle n'est pas remplie, dégrade
silencieusement vers nbd/nbdssl avec InitEx renvoyant quand même VIX_OK :

  1. Le chemin passé doit être la RACINE de la distribution, pas son
     sous-répertoire lib64 : VDDK y concatène « lib64/ » lui-même.
  2. LD_LIBRARY_PATH doit contenir lib64 AVANT le démarrage du processus.
     Le VDDK ne pose aucun RPATH/RUNPATH, donc les dépendances du plugin
     (libvapi-core.so.2, libvmacore.so, libvmomi.so…) restent introuvables.
     Le définir depuis os.environ en cours d'exécution est sans effet, et
     précharger les .so en RTLD_GLOBAL provoque un SIGSEGV — ne pas essayer.

     Lancer par :  LD_LIBRARY_PATH=<distrib>/lib64 python3 …
     ou via Environment= dans l'unité systemd / ENV dans le Dockerfile.

Aucune connexion à vCenter n'est établie : ce script valide uniquement
l'environnement local.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import subprocess
import sys
from ctypes import c_char_p, c_size_t, c_uint32, c_uint64, c_void_p

VIX_OK = 0
VDDK_MAJOR, VDDK_MINOR = 8, 0

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    GREEN = RED = YELLOW = DIM = RESET = ""


def ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET}    {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}ECHEC{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}ATTN{RESET}  {msg}")


class Probe:
    def __init__(self, distrib: str, verbose: bool = False) -> None:
        self.distrib = os.path.abspath(distrib.rstrip("/"))
        self.lib64 = os.path.join(self.distrib, "lib64")
        self.verbose = verbose
        self.messages: list[str] = []
        self.errors = 0
        self.lib: ctypes.CDLL | None = None
        self._libc = ctypes.CDLL("libc.so.6")
        self._libc.vsnprintf.restype = ctypes.c_int
        self._libc.vsnprintf.argtypes = [c_char_p, c_size_t, c_char_p, c_void_p]
        self._keepalive: list = []

    # -- 1. environnement ------------------------------------------------
    def check_host(self) -> None:
        print("\n1. Hôte")
        machine = platform.machine()
        if machine == "x86_64":
            ok(f"architecture {machine}")
        else:
            fail(f"architecture {machine} — le VDDK n'existe qu'en x86-64")
            self.errors += 1
        try:
            ver = os.confstr("CS_GNU_LIBC_VERSION") or "inconnue"
            ok(f"libc {ver} (VDDK 8.0.3 exige glibc >= 2.16)")
        except (ValueError, OSError):
            warn("version de glibc indéterminable")

    # -- 2. distribution -------------------------------------------------
    def check_layout(self) -> None:
        print("\n2. Distribution VDDK")
        if os.path.basename(self.distrib) == "lib64":
            fail(f"{self.distrib} est le sous-répertoire lib64.")
            print(f"        {DIM}Passez la racine : {os.path.dirname(self.distrib)}{RESET}")
            self.errors += 1
            return
        soname = os.path.join(self.lib64, "libvixDiskLib.so.8")
        plugin = os.path.join(self.lib64, "libdiskLibPlugin.so")
        for label, path in (("libvixDiskLib.so.8", soname),
                            ("libdiskLibPlugin.so", plugin)):
            if os.path.exists(path):
                ok(label)
            else:
                fail(f"{label} absent de {self.lib64}")
                self.errors += 1
        ldpath = os.environ.get("LD_LIBRARY_PATH", "")
        if any(os.path.abspath(p) == self.lib64 for p in ldpath.split(":") if p):
            ok("LD_LIBRARY_PATH contient lib64")
        else:
            fail("LD_LIBRARY_PATH ne contient pas lib64 — le plugin de transport "
                 "avancé (san, hotadd) ne pourra pas se charger")
            print(f"        {DIM}relancer : LD_LIBRARY_PATH={self.lib64} python3 {sys.argv[0]} …{RESET}")
            self.errors += 1

        if os.path.exists(soname):
            missing = self._ldd_missing(soname)
            if missing:
                fail(f"dépendances non résolues : {', '.join(missing)}")
                self.errors += 1
            else:
                ok("toutes les dépendances partagées sont résolues")

    @staticmethod
    def _ldd_missing(path: str) -> list[str]:
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = os.path.dirname(path)
        try:
            out = subprocess.run(["ldd", path], capture_output=True, text=True,
                                 env=env, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [ln.split()[0] for ln in out.splitlines() if "not found" in ln]

    # -- 3. chargement ---------------------------------------------------
    def load(self) -> bool:
        print("\n3. Chargement et initialisation")
        try:
            self.lib = ctypes.CDLL(os.path.join(self.lib64, "libvixDiskLib.so.8"),
                                   mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            fail(f"dlopen : {exc}")
            self.errors += 1
            return False
        ok("bibliothèque chargée")

        lib = self.lib
        logfunc = ctypes.CFUNCTYPE(None, c_char_p, c_void_p)
        lib.VixDiskLib_InitEx.restype = c_uint64
        lib.VixDiskLib_InitEx.argtypes = [c_uint32, c_uint32, logfunc, logfunc,
                                          logfunc, c_char_p, c_char_p]
        lib.VixDiskLib_ListTransportModes.restype = c_char_p
        lib.VixDiskLib_GetErrorText.restype = c_char_p
        lib.VixDiskLib_GetErrorText.argtypes = [c_uint64, c_char_p]
        lib.VixDiskLib_AllocateConnectParams.restype = c_void_p
        lib.VixDiskLib_FreeConnectParams.argtypes = [c_void_p]
        lib.VixDiskLib_Exit.restype = None

        def make(level: str):
            def cb(fmt, ap):
                buf = ctypes.create_string_buffer(4096)
                self._libc.vsnprintf(buf, 4096, fmt, ap)
                self.messages.append(f"{level} {buf.value.decode(errors='replace').strip()}")
            wrapped = logfunc(cb)
            self._keepalive.append(wrapped)   # sinon le GC libère le trampoline
            return wrapped

        err = lib.VixDiskLib_InitEx(VDDK_MAJOR, VDDK_MINOR,
                                    make("[log ]"), make("[warn]"), make("[!!!!]"),
                                    self.distrib.encode(), None)
        if err != VIX_OK:
            text = lib.VixDiskLib_GetErrorText(err, None)
            fail(f"InitEx -> 0x{err:x} {text.decode() if text else ''}")
            self.errors += 1
            return False
        ok(f"InitEx(libDir={self.distrib}) -> VIX_OK")

        params = lib.VixDiskLib_AllocateConnectParams()
        if params:
            ok("AllocateConnectParams")
            lib.VixDiskLib_FreeConnectParams(params)
        else:
            fail("AllocateConnectParams a renvoyé NULL")
            self.errors += 1
        return True

    # -- 4. transports ---------------------------------------------------
    def check_transports(self, required: list[str]) -> None:
        print("\n4. Modes de transport")
        assert self.lib is not None
        raw = self.lib.VixDiskLib_ListTransportModes()
        modes = raw.decode().split(":") if raw else []
        print(f"        disponibles : {':'.join(modes) or '(aucun)'}")

        if any("Advanced transport plugin" in m and "successfully loaded" in m
               for m in self.messages):
            ok("plugin de transport avancé chargé")
        elif any("Advanced transport module not loaded" in m for m in self.messages):
            fail("plugin de transport avancé NON chargé — libDir probablement erroné")
            self.errors += 1

        for mode in required:
            if mode in modes:
                ok(f"« {mode} » disponible")
            else:
                fail(f"« {mode} » indisponible sur cet hôte")
                self.errors += 1

        if "hotadd" in modes and "hotadd" not in required:
            print(f"        {DIM}hotadd listé : la bibliothèque le supporte, mais il exige "
                  f"que cet hôte soit une VM du cluster source.{RESET}")

    def dump_log(self) -> None:
        if not self.verbose:
            return
        print(f"\n{DIM}--- journal VDDK ({len(self.messages)} lignes) ---{RESET}")
        for m in self.messages:
            print(f"{DIM}  {m}{RESET}")

    def close(self) -> None:
        if self.lib is not None:
            self.lib.VixDiskLib_Exit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("distrib", help="racine de vmware-vix-disklib-distrib")
    ap.add_argument("--require", default="nbdssl",
                    help="modes exigés, séparés par des virgules (défaut : nbdssl)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="afficher le journal VDDK complet")
    args = ap.parse_args()

    print(f"{DIM}VDDK probe — {args.distrib}{RESET}")
    probe = Probe(args.distrib, args.verbose)
    probe.check_host()
    probe.check_layout()
    if probe.errors == 0 and probe.load():
        probe.check_transports([m.strip() for m in args.require.split(",") if m.strip()])
    probe.dump_log()
    probe.close()

    print()
    if probe.errors:
        print(f"{RED}{probe.errors} contrôle(s) en échec — environnement inapte.{RESET}")
        return 1
    print(f"{GREEN}Environnement VDDK conforme.{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
