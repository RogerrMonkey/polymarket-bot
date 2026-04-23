from __future__ import annotations

import json
from pathlib import Path

from prediction_bot import health_check, live_readiness


# ---- health-check --------------------------------------------------------


def test_collect_health_empty_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.delenv("BOT_LIVE_MODE", raising=False)
    monkeypatch.setattr(health_check, "check_warp_active", lambda: False)
    monkeypatch.setattr(health_check, "_analyst_label", lambda: "groq (llama-3.3-70b-versatile)")

    data = health_check.collect_health(
        workspace_root=tmp_path,
        db_path=str(tmp_path / "db.sqlite"),
    )

    assert data["warp_active"] is False
    assert data["analyst"].startswith("groq")
    assert data["paper_days"] == 0
    assert data["last_run"] == "never"
    assert data["kill_switch"] is False
    assert data["live_mode"] is False


def test_status_line_branches() -> None:
    accumulating = {
        "live_mode": False, "kill_switch": False, "paper_days": 6,
    }
    assert "ACCUMULATING" in health_check._status_line(accumulating)

    mid = {"live_mode": False, "kill_switch": False, "paper_days": 20}
    assert "PAPER MODE" in health_check._status_line(mid)

    kill = {"live_mode": False, "kill_switch": True, "paper_days": 99}
    assert "KILL SWITCH" in health_check._status_line(kill)

    live = {"live_mode": True, "kill_switch": False, "paper_days": 99}
    assert "LIVE MODE" in health_check._status_line(live)


def test_print_health_runs_cleanly(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.delenv("BOT_LIVE_MODE", raising=False)
    monkeypatch.setattr(health_check, "check_warp_active", lambda: True)
    monkeypatch.setattr(health_check, "_analyst_label", lambda: "groq (x)")
    data = health_check.collect_health(tmp_path, str(tmp_path / "db.sqlite"))
    health_check.print_health(data)
    out = capsys.readouterr().out
    assert "System Health" in out
    assert "WARP active" in out
    assert "Status:" in out


# ---- live-readiness ------------------------------------------------------


def test_readiness_all_fail_on_empty_workspace(tmp_path: Path, monkeypatch) -> None:
    for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS", "SIGNATURE_TYPE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.delenv("BOT_LIVE_MODE", raising=False)

    gates = live_readiness.collect_readiness(
        workspace_root=tmp_path,
        db_path=str(tmp_path / "db.sqlite"),
    )
    names_failing = {g.name for g in gates if not g.passed}
    # Auth, paper days, brier, win rate — all fail with an empty workspace.
    assert "Auth verified" in names_failing
    assert "Paper days: >= 30" in names_failing
    assert "Brier score < 0.22 (n>=20)" in names_failing
    assert "Win rate > 52% (n>=10)" in names_failing


def test_readiness_kill_switch_gate(monkeypatch) -> None:
    monkeypatch.setenv("KILL_SWITCH", "true")
    gate = live_readiness._kill_switch_gate()
    assert gate.passed is False
    assert gate.detail == "ON"

    monkeypatch.setenv("KILL_SWITCH", "false")
    assert live_readiness._kill_switch_gate().passed is True


def test_readiness_live_mode_gate(monkeypatch) -> None:
    monkeypatch.setenv("BOT_LIVE_MODE", "true")
    g = live_readiness._live_mode_off_gate()
    assert g.passed is False
    assert "already live" in g.detail


def test_readiness_returns_nonzero_exit_when_failing(tmp_path: Path, monkeypatch, capsys) -> None:
    for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS", "SIGNATURE_TYPE"):
        monkeypatch.delenv(k, raising=False)
    rc = live_readiness.run_live_readiness_command(tmp_path, str(tmp_path / "db.sqlite"))
    assert rc == 1
    out = capsys.readouterr().out
    assert "VERDICT: NOT READY" in out
    assert "Earliest ready" in out


def test_build_readiness_payload_shape(tmp_path: Path, monkeypatch) -> None:
    for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_FUNDER_ADDRESS", "SIGNATURE_TYPE"):
        monkeypatch.setenv(k, "x")
    payload = live_readiness.build_readiness_payload(tmp_path, str(tmp_path / "db.sqlite"))
    assert "ready" in payload
    assert "gates" in payload
    assert isinstance(payload["gates"], list)
    # Auth gate should PASS since we set the env vars above
    auth = next(g for g in payload["gates"] if g["name"] == "Auth verified")
    assert auth["passed"] is True


# ---- run.lock + heartbeat ------------------------------------------------


def test_run_lock_acquire_and_release(tmp_path: Path) -> None:
    from prediction_bot.main_loop import (
        _acquire_run_lock,
        _release_run_lock,
        _run_lock_path,
    )

    assert _acquire_run_lock(tmp_path, cycles_planned=5) is True
    lock_path = _run_lock_path(tmp_path)
    assert lock_path.exists()
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert payload["cycles_planned"] == 5
    assert isinstance(payload["pid"], int)

    _release_run_lock(tmp_path)
    assert not lock_path.exists()


def test_run_lock_stale_is_cleared(tmp_path: Path) -> None:
    from prediction_bot.main_loop import _acquire_run_lock, _run_lock_path

    lock_path = _run_lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # PID 999999 very unlikely to be alive
    lock_path.write_text(json.dumps({"pid": 999999, "started_at": "x", "cycles_planned": 1}), encoding="utf-8")
    assert _acquire_run_lock(tmp_path, cycles_planned=2) is True
    # A scheduler_health row with status=crashed should have been written
    health = (tmp_path / "data" / "scheduler_health.jsonl").read_text(encoding="utf-8")
    assert '"status": "crashed"' in health


def test_append_heartbeat_writes_row(tmp_path: Path) -> None:
    from prediction_bot.main_loop import _append_heartbeat

    _append_heartbeat(tmp_path, cycle=3, analyses_so_far=12)
    rows = (tmp_path / "data" / "scheduler_health.jsonl").read_text(encoding="utf-8").splitlines()
    payload = json.loads(rows[-1])
    assert payload["type"] == "heartbeat"
    assert payload["cycle"] == 3
    assert payload["analyses_so_far"] == 12
