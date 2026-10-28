"""Mesures de la VM relais (tools/relay_metrics.py) sur un faux /proc."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from relay_metrics import RelayMetrics

NET_HEAD = "Inter-|   Receive\n face |bytes\n"


def write_proc(root: Path, *, cpu: str, rx: int, tx: int, vdb: tuple[int, int, int, int]) -> None:
    (root / "net").mkdir(parents=True, exist_ok=True)
    (root / "stat").write_text(f"cpu  {cpu}\ncpu0 0 0 0 0\n")
    (root / "meminfo").write_text("MemTotal: 8388608 kB\nMemFree: 1 kB\nMemAvailable: 2097152 kB\n")
    (root / "net" / "dev").write_text(
        NET_HEAD
        + "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"
        + f"  eth0: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0\n")
    r, rs, w, ws = vdb
    (root / "diskstats").write_text(
        "   7 0 loop0 50 0 50 0 0 0 0 0 0 0 0\n"
        f" 252 16 vdb {r} 0 {rs} 0 {w} 0 {ws} 0 0 0 0\n"
        " 252 17 vdb1 1000 0 1000 0 1000 0 1000 0 0 0 0\n")


def test_debits_entre_deux_pouls(tmp_path, monkeypatch):
    proc, block = tmp_path / "proc", tmp_path / "block"
    (block / "vdb").mkdir(parents=True)
    clock = iter([100.0, 110.0])
    monkeypatch.setattr("relay_metrics.time.monotonic", lambda: next(clock))

    m = RelayMetrics(proc, block)
    write_proc(proc, cpu="100 0 100 800 0 0 0 0 0 0", rx=0, tx=0, vdb=(0, 0, 0, 0))
    first = m.sample()
    assert first["cpu_percent"] is None and first["net_rx_bps"] is None
    assert first["mem_total_mb"] == 8192 and first["mem_used_mb"] == 6144
    assert 0 < first["fs_used_mb"] <= first["fs_total_mb"]

    write_proc(proc, cpu="400 0 400 1200 0 0 0 0 0 0", rx=10 * 2 ** 20, tx=1000,
               vdb=(100, 2048, 500, 20480))
    second = m.sample()
    assert second["cpu_percent"] == 60.0           # 600 occupés sur 1000
    assert second["net_rx_bps"] == 2 ** 20         # lo ignoré
    assert second["net_tx_bps"] == 100
    assert second["disk_read_iops"] == 10          # partition vdb1 et loop ignorées
    assert second["disk_write_iops"] == 50
    assert second["disk_write_bps"] == 20480 * 512 / 10
