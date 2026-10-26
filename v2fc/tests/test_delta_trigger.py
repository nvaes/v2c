"""Delta manuel ou quotidien à heure fixe (26/10/2026)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routers.jobs import _consume_delta_trigger, _next_daily  # noqa: E402


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_next_daily_same_day_or_next_day_in_local_time():
    # 26/10/2026 10:00 UTC = 11:00 à Bruxelles (heure d'hiver, UTC+1)
    after = datetime(2026, 10, 26, 10, 0)
    assert _next_daily("22:00", "Europe/Brussels", after) == datetime(2026, 10, 26, 21, 0)
    assert _next_daily("02:00", "Europe/Brussels", after) == datetime(2026, 10, 27, 1, 0)


def test_next_daily_follows_daylight_saving_change():
    # 02:00 à Bruxelles : 00:00 UTC en été (24/10), 01:00 UTC en hiver (26/10)
    after = datetime(2026, 10, 23, 23, 0)
    assert _next_daily("02:00", "Europe/Brussels", after) == datetime(2026, 10, 24, 0, 0)
    after = datetime(2026, 10, 25, 12, 0)
    assert _next_daily("02:00", "Europe/Brussels", after) == datetime(2026, 10, 26, 1, 0)


def test_manual_request_is_served_and_future_schedule_kept():
    later = _now() + timedelta(hours=3)
    job = SimpleNamespace(delta_requested=True, delta_scheduled_at=later,
                          delta_daily_time="02:00", delta_timezone="Europe/Brussels")
    _consume_delta_trigger(job)
    assert job.delta_requested is False
    assert job.delta_scheduled_at == later


def test_due_daily_schedule_moves_to_next_occurrence():
    job = SimpleNamespace(delta_requested=False, delta_scheduled_at=_now() - timedelta(days=2),
                          delta_daily_time="02:00", delta_timezone="Europe/Brussels")
    _consume_delta_trigger(job)
    assert _now() < job.delta_scheduled_at <= _now() + timedelta(days=1)
