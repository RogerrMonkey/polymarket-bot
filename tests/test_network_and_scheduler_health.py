from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from prediction_bot.scheduler_health import (
    read_scheduler_health,
    record_cycle_health,
    success_rate,
)
from prediction_bot.utils.network import check_warp_active


# --- WARP detection ---


def test_check_warp_active_returns_true_when_dns_resolves(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", 443))],
    )
    assert check_warp_active(host="example.com", timeout_seconds=1.0) is True


def test_check_warp_active_returns_false_on_dns_failure(monkeypatch) -> None:
    def _raise(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    assert check_warp_active(host="nonexistent.invalid", timeout_seconds=0.5) is False


def test_check_warp_active_never_raises(monkeypatch) -> None:
    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(socket, "getaddrinfo", _raise)
    assert check_warp_active(host="weird.invalid") is False


# --- Scheduler health ---


def _seed_analyses(root: Path, count: int, day: str) -> None:
    path = root / "data" / "analyses.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for i in range(count):
            fh.write(json.dumps({"timestamp": f"{day}T09:00:0{i}Z", "market_id": f"m-{i}"}) + "\n")


def test_record_cycle_health_ok_when_analyses_present(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date().isoformat()
    _seed_analyses(tmp_path, count=3, day=today)
    payload = record_cycle_health(tmp_path, warp_active=True)
    assert payload["status"] == "ok"
    assert payload["analyses_today"] == 3
    assert payload["warp_active"] is True


def test_record_cycle_health_missed_when_no_analyses(tmp_path: Path) -> None:
    payload = record_cycle_health(tmp_path, warp_active=False)
    assert payload["status"] == "missed"
    assert payload["reason"] == "no_analyses_warp_inactive"
    assert payload["analyses_today"] == 0


def test_success_rate_returns_none_when_no_entries(tmp_path: Path) -> None:
    rate, ok, total = success_rate(tmp_path, window=14)
    assert rate is None
    assert ok == 0
    assert total == 0


def test_success_rate_over_window(tmp_path: Path) -> None:
    # 4 ok, 1 missed → rate=0.8
    for i in range(4):
        record_cycle_health(tmp_path, warp_active=True)  # missed since no analyses seeded
    # Manually rewrite the file to mix ok/missed
    path = tmp_path / "data" / "scheduler_health.jsonl"
    lines = [
        json.dumps({"date": "2026-04-17", "status": "ok", "warp_active": True, "analyses_today": 3, "reason": "ok", "timestamp": "t"}),
        json.dumps({"date": "2026-04-18", "status": "ok", "warp_active": True, "analyses_today": 3, "reason": "ok", "timestamp": "t"}),
        json.dumps({"date": "2026-04-19", "status": "ok", "warp_active": True, "analyses_today": 3, "reason": "ok", "timestamp": "t"}),
        json.dumps({"date": "2026-04-20", "status": "ok", "warp_active": True, "analyses_today": 3, "reason": "ok", "timestamp": "t"}),
        json.dumps({"date": "2026-04-21", "status": "missed", "warp_active": False, "analyses_today": 0, "reason": "no_analyses", "timestamp": "t"}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rate, ok, total = success_rate(tmp_path, window=14)
    assert total == 5
    assert ok == 4
    assert rate == pytest.approx(0.8)


def test_read_scheduler_health_respects_limit(tmp_path: Path) -> None:
    path = tmp_path / "data" / "scheduler_health.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [json.dumps({"date": f"2026-04-{i:02d}", "status": "ok"}) for i in range(1, 11)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    latest = read_scheduler_health(tmp_path, limit=3)
    assert len(latest) == 3
    assert latest[-1]["date"] == "2026-04-10"
