from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from prediction_bot import main_loop
from prediction_bot.main_loop import (
    _build_scheduled_job,
    _parse_schedule_time,
    run_paper_loop_scheduled,
)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #


class _FakeTrigger:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict] = []
        self.started = False
        self.shutdown_called = False

    def add_job(self, func, trigger, **kwargs):  # noqa: ANN001
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})

    def start(self) -> None:
        self.started = True

    def shutdown(self, wait: bool = True) -> None:  # noqa: ARG002
        self.shutdown_called = True


def _scheduler_factory() -> tuple:
    return _FakeScheduler(), _FakeTrigger


# --------------------------------------------------------------------------- #
# _parse_schedule_time                                                         #
# --------------------------------------------------------------------------- #


def test_parse_schedule_time_valid() -> None:
    assert _parse_schedule_time("08:00") == (8, 0)
    assert _parse_schedule_time("23:59") == (23, 59)
    assert _parse_schedule_time("00:00") == (0, 0)


def test_parse_schedule_time_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        _parse_schedule_time("8")
    with pytest.raises(ValueError):
        _parse_schedule_time("25:00")
    with pytest.raises(ValueError):
        _parse_schedule_time("12:60")


# --------------------------------------------------------------------------- #
# _build_scheduled_job — exception isolation                                   #
# --------------------------------------------------------------------------- #


def test_scheduled_job_swallows_exceptions(monkeypatch, tmp_path: Path) -> None:
    """A failing run must NOT propagate — scheduler must keep firing."""
    call_count = {"n": 0}

    def _boom(**kwargs):  # noqa: ANN003, ARG001
        call_count["n"] += 1
        raise RuntimeError("simulated paper-loop failure")

    monkeypatch.setattr(main_loop, "run_paper_loop", _boom)

    job = _build_scheduled_job(
        cycles=1,
        interval_seconds=30,
        limit_per_venue=10,
        top_n_for_risk=3,
        workspace_root=tmp_path,
        dry_run=True,
    )

    # Must NOT raise
    job()
    assert call_count["n"] == 1


def test_scheduled_job_invokes_paper_loop(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def _ok(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(main_loop, "run_paper_loop", _ok)

    job = _build_scheduled_job(
        cycles=2,
        interval_seconds=15,
        limit_per_venue=20,
        top_n_for_risk=5,
        workspace_root=tmp_path,
        dry_run=True,
    )
    job()

    assert captured["cycles"] == 2
    assert captured["interval_seconds"] == 15
    assert captured["limit_per_venue"] == 20
    assert captured["top_n_for_risk"] == 5
    assert captured["dry_run"] is True
    assert captured["workspace_root"] == tmp_path


# --------------------------------------------------------------------------- #
# run_paper_loop_scheduled — full path with mocked scheduler                   #
# --------------------------------------------------------------------------- #


def test_scheduled_runner_registers_job_and_shuts_down_cleanly(
    monkeypatch, tmp_path: Path
) -> None:
    fake = _FakeScheduler()

    # Stop loop.run_forever() immediately by raising KeyboardInterrupt
    class _FakeLoop:
        def run_forever(self) -> None:
            raise KeyboardInterrupt()

        def close(self) -> None:
            pass

    monkeypatch.setattr(main_loop.asyncio, "new_event_loop", lambda: _FakeLoop())
    monkeypatch.setattr(main_loop.asyncio, "set_event_loop", lambda _loop: None)

    def _factory() -> tuple:
        return fake, _FakeTrigger

    exit_code = run_paper_loop_scheduled(
        schedule_kind="daily",
        schedule_time="09:30",
        cycles=1,
        interval_seconds=30,
        limit_per_venue=10,
        top_n_for_risk=3,
        workspace_root=tmp_path,
        dry_run=True,
        scheduler_factory=_factory,
    )

    assert exit_code == 0
    assert fake.started is True
    assert fake.shutdown_called is True
    assert len(fake.jobs) == 1
    job = fake.jobs[0]
    assert job["id"] == "paper_loop_daily"
    assert job["replace_existing"] is True
    assert isinstance(job["trigger"], _FakeTrigger)
    assert job["trigger"].kwargs == {"hour": 9, "minute": 30, "timezone": "UTC"}


def test_scheduled_runner_rejects_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_paper_loop_scheduled(
            schedule_kind="hourly",
            schedule_time="08:00",
            cycles=1,
            interval_seconds=30,
            limit_per_venue=10,
            top_n_for_risk=3,
            workspace_root=tmp_path,
            dry_run=True,
            scheduler_factory=_scheduler_factory,
        )
