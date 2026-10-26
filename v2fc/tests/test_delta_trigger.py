"""Delta manuel / planifié : consommation au démarrage d'un delta (26/10/2026)."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.routers.jobs import _consume_delta_trigger  # noqa: E402


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_manual_request_is_served_and_future_schedule_kept():
    later = _now() + timedelta(hours=3)
    job = SimpleNamespace(delta_requested=True, delta_scheduled_at=later, delta_repeat_daily=False)
    _consume_delta_trigger(job)
    assert job.delta_requested is False
    assert job.delta_scheduled_at == later


def test_due_one_shot_schedule_is_cleared():
    job = SimpleNamespace(delta_requested=False, delta_scheduled_at=_now() - timedelta(minutes=1),
                          delta_repeat_daily=False)
    _consume_delta_trigger(job)
    assert job.delta_scheduled_at is None


def test_due_daily_schedule_moves_to_next_future_day_same_time():
    due = _now() - timedelta(days=2, minutes=5)       # runner arrêté deux jours
    job = SimpleNamespace(delta_requested=False, delta_scheduled_at=due, delta_repeat_daily=True)
    _consume_delta_trigger(job)
    assert job.delta_scheduled_at == due + timedelta(days=3)
    assert job.delta_scheduled_at > _now()
