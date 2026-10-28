"""
Mesures de charge de la VM relais (27/10/2026) : CPU, RAM, réseau, IOPS
disque et espace disque (28/10/2026), lues dans /proc par le runner et jointes à son pouls (toutes les
15 s) pour le cadre « VM relais » de la page Migration.

Le conteneur runner est privilégié mais garde son propre espace réseau :
/proc/stat, /proc/meminfo et /proc/diskstats décrivent toute la VM, alors
que /proc/net/dev ne voit que le trafic du conteneur — c'est-à-dire celui
des copies (lecture VDDK depuis la source), l'essentiel du trafic de la VM.
L'espace disque est celui du système de fichiers de « / » du conteneur :
l'overlay Docker, donc la partition de la VM qui porte /var/lib/docker
(images, volume d'état du runner, logs, cache libguestfs).

Les débits sont des moyennes entre deux appels de `RelayMetrics.sample()` :
le premier appel ne renvoie que la RAM et le nombre de vCPU.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

SECTOR = 512  # /proc/diskstats compte en secteurs de 512 octets, quel que soit le disque

# Périphériques blocs qui ne sont pas des disques de la VM.
_SKIP_PREFIXES = ("loop", "ram", "sr", "zram", "dm-", "md", "nbd")


def _cpu_times(proc: Path) -> tuple[int, int]:
    """(total, inactif) en jiffies depuis le boot ; iowait compte comme inactif."""
    fields = [int(x) for x in (proc / "stat").read_text().splitlines()[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
    # guest/guest_nice (9e et 10e) sont déjà inclus dans user/nice
    return sum(fields[:8]), idle


def _meminfo(proc: Path) -> dict[str, int]:
    out = {}
    for line in (proc / "meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        out[key] = int(rest.split()[0])          # kB
    return out


def _net_bytes(proc: Path) -> tuple[int, int]:
    rx = tx = 0
    for line in (proc / "net" / "dev").read_text().splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == "lo":
            continue
        cols = rest.split()
        rx += int(cols[0])
        tx += int(cols[8])
    return rx, tx


def _disk_counters(proc: Path, sys_block: Path) -> tuple[int, int, int, int]:
    """(lectures, écritures, octets lus, octets écrits) des disques entiers."""
    whole = {p.name for p in sys_block.iterdir()} if sys_block.is_dir() else None
    reads = writes = rbytes = wbytes = 0
    for line in (proc / "diskstats").read_text().splitlines():
        cols = line.split()
        name = cols[2]
        if name.startswith(_SKIP_PREFIXES):
            continue
        # /sys/block ne liste que les disques, pas leurs partitions
        if whole is not None and name not in whole:
            continue
        reads += int(cols[3])
        rbytes += int(cols[5]) * SECTOR
        writes += int(cols[7])
        wbytes += int(cols[9]) * SECTOR
    return reads, writes, rbytes, wbytes


def _fs_usage(path: Path) -> tuple[int, int]:
    """(total, utilisé) en Mio, comme df : utilisé = blocs - blocs libres."""
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    return total // 2 ** 20, used // 2 ** 20


class RelayMetrics:
    def __init__(self, proc: Path = Path("/proc"), sys_block: Path = Path("/sys/block"),
                 fs_path: Path = Path("/")) -> None:
        self.proc, self.sys_block, self.fs_path = proc, sys_block, fs_path
        self._prev: tuple | None = None

    def _read(self) -> tuple:
        return (time.monotonic(), _cpu_times(self.proc), _net_bytes(self.proc),
                _disk_counters(self.proc, self.sys_block))

    def sample(self) -> dict:
        mem = _meminfo(self.proc)
        total_kb = mem.get("MemTotal", 0)
        fs_total, fs_used = _fs_usage(self.fs_path)
        out: dict = {
            "cpu_count": os.cpu_count() or 0,
            "cpu_percent": None,
            "mem_total_mb": total_kb // 1024,
            "mem_used_mb": (total_kb - mem.get("MemAvailable", mem.get("MemFree", 0))) // 1024,
            "net_rx_bps": None, "net_tx_bps": None,
            "disk_read_iops": None, "disk_write_iops": None,
            "disk_read_bps": None, "disk_write_bps": None,
            "fs_total_mb": fs_total, "fs_used_mb": fs_used,
        }
        cur = self._read()
        prev, self._prev = self._prev, cur
        if prev is None:
            return out
        dt = cur[0] - prev[0]
        if dt <= 0:
            return out
        (total, idle), (ptotal, pidle) = cur[1], prev[1]
        if total > ptotal:
            out["cpu_percent"] = round(100 * (1 - (idle - pidle) / (total - ptotal)), 1)

        def rate(a: int, b: int) -> float:
            # compteur remis à zéro (interface recréée...) : pas de valeur négative
            return round(max(0, a - b) / dt, 1)

        out["net_rx_bps"] = rate(cur[2][0], prev[2][0])
        out["net_tx_bps"] = rate(cur[2][1], prev[2][1])
        for i, key in enumerate(("disk_read_iops", "disk_write_iops",
                                 "disk_read_bps", "disk_write_bps")):
            out[key] = rate(cur[3][i], prev[3][i])
        return out
