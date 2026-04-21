from __future__ import annotations

from pathlib import Path

import prediction_bot.checklist as checklist


def test_collect_pre_live_checks_aggregates(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(checklist, "_check_dry_run_false", lambda: checklist.ChecklistItem("dry", True, "ok"))
    monkeypatch.setattr(checklist, "_check_polygon_rpc", lambda: checklist.ChecklistItem("rpc", True, "ok"))
    monkeypatch.setattr(checklist, "_check_polymarket_envs", lambda: [checklist.ChecklistItem("poly", True, "ok")])
    monkeypatch.setattr(
        checklist,
        "_check_analyst_provider_resolved",
        lambda: checklist.ChecklistItem("analyst", True, "ok"),
    )
    monkeypatch.setattr(
        checklist,
        "_check_balance_and_open_orders",
        lambda: [
            checklist.ChecklistItem("balance", True, "ok"),
            checklist.ChecklistItem("orders", True, "ok"),
        ],
    )
    monkeypatch.setattr(checklist, "_check_risk_config", lambda root: [checklist.ChecklistItem("risk", True, "ok")])
    monkeypatch.setattr(checklist, "_check_paper_gates", lambda root, db_path: [checklist.ChecklistItem("gates", True, "ok")])
    monkeypatch.setattr(
        checklist,
        "_check_paper_loop_has_run_today",
        lambda root: checklist.ChecklistItem("loop_today", True, "ok"),
    )
    monkeypatch.setattr(
        checklist,
        "_check_news_feed_has_sources",
        lambda root: checklist.ChecklistItem("news_sources", True, "ok"),
    )
    monkeypatch.setattr(
        checklist,
        "_check_scheduled_job_registered",
        lambda: checklist.ChecklistItem("scheduled_job_registered", True, "ok"),
    )
    monkeypatch.setattr(
        checklist,
        "_check_wallet_address_valid",
        lambda: checklist.ChecklistItem("wallet_address_valid", True, "ok"),
    )
    monkeypatch.setattr(
        checklist,
        "_check_all_safety_checks_pass",
        lambda: checklist.ChecklistItem("all_safety_checks_pass", True, "ok"),
    )
    monkeypatch.setattr(
        checklist,
        "_check_scheduler_success_rate",
        lambda root: checklist.ChecklistItem("scheduler_success_rate_gte_80", True, "ok"),
    )
    monkeypatch.setattr(checklist, "_check_access", lambda: [checklist.ChecklistItem("access", True, "ok")])

    ready, items = checklist.run_pre_live_checklist(workspace_root=tmp_path, db_path=str(tmp_path / "db.sqlite"))

    assert ready is True
    assert len(items) == 15


def test_check_wallet_address_valid_missing(monkeypatch) -> None:
    monkeypatch.delenv("POLYGON_WALLET_ADDRESS", raising=False)
    item = checklist._check_wallet_address_valid()
    assert item.passed is False
    assert "missing" in item.detail


def test_check_wallet_address_valid_checksums_lowercase(monkeypatch) -> None:
    # Lowercase valid address → normalized to EIP-55 mixed case, still PASS
    monkeypatch.setenv(
        "POLYGON_WALLET_ADDRESS",
        "0x8ba1f109551bd432803012645ac136ddd64dba72",
    )
    item = checklist._check_wallet_address_valid()
    assert item.passed is True
    assert "normalized_to:" in item.detail or "checksum_match" in item.detail


def test_check_wallet_address_valid_rejects_garbage(monkeypatch) -> None:
    monkeypatch.setenv("POLYGON_WALLET_ADDRESS", "not-an-address")
    item = checklist._check_wallet_address_valid()
    assert item.passed is False
    assert "invalid" in item.detail


def test_check_all_safety_checks_pass_happy(monkeypatch) -> None:
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    monkeypatch.setenv("BOT_LIVE_MODE", "false")
    monkeypatch.setenv("DRY_RUN", "true")
    item = checklist._check_all_safety_checks_pass()
    assert item.passed is True
    assert "live_mode_false" in item.detail


def test_check_all_safety_checks_pass_fails_on_live_mode(monkeypatch) -> None:
    monkeypatch.setenv("BOT_LIVE_MODE", "true")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("KILL_SWITCH", raising=False)
    item = checklist._check_all_safety_checks_pass()
    assert item.passed is False
    assert "live_mode_true" in item.detail


def test_check_all_safety_checks_pass_fails_on_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("KILL_SWITCH", "true")
    monkeypatch.setenv("BOT_LIVE_MODE", "false")
    monkeypatch.setenv("DRY_RUN", "true")
    item = checklist._check_all_safety_checks_pass()
    assert item.passed is False
    assert "kill_switch_env_active" in item.detail


def test_run_pre_live_checklist_fails_on_any_check(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(checklist, "collect_pre_live_checks", lambda workspace_root, db_path: [
        checklist.ChecklistItem("one", True, "ok"),
        checklist.ChecklistItem("two", False, "bad"),
    ])

    ready, items = checklist.run_pre_live_checklist(workspace_root=tmp_path, db_path="x")
    assert ready is False
    assert len(items) == 2


def test_check_scheduled_job_registered_skips_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Linux")
    item = checklist._check_scheduled_job_registered()
    assert item.name == "scheduled_job_registered"
    assert item.passed is True
    assert "non_windows_skip" in item.detail


def test_check_scheduled_job_registered_pass(monkeypatch) -> None:
    import subprocess as _sp

    monkeypatch.setattr("platform.system", lambda: "Windows")

    class _CompletedOK:
        returncode = 0
        stdout = "TaskName: PolymarketPaperLoop\nStatus:    Ready\n"
        stderr = ""

    monkeypatch.setattr(_sp, "run", lambda *a, **kw: _CompletedOK())
    item = checklist._check_scheduled_job_registered()
    assert item.passed is True
    assert "registered" in item.detail.lower()
    assert "Ready" in item.detail


def test_check_scheduled_job_registered_fail_not_registered(monkeypatch) -> None:
    import subprocess as _sp

    monkeypatch.setattr("platform.system", lambda: "Windows")

    class _CompletedFail:
        returncode = 1
        stdout = ""
        stderr = "ERROR: The system cannot find the file specified.\n"

    monkeypatch.setattr(_sp, "run", lambda *a, **kw: _CompletedFail())
    item = checklist._check_scheduled_job_registered()
    assert item.passed is False
    assert "not_registered" in item.detail


def test_check_scheduled_job_registered_handles_missing_schtasks(monkeypatch) -> None:
    import subprocess as _sp

    monkeypatch.setattr("platform.system", lambda: "Windows")

    def _raise(*a, **kw):
        raise FileNotFoundError("schtasks not on PATH")

    monkeypatch.setattr(_sp, "run", _raise)
    item = checklist._check_scheduled_job_registered()
    assert item.passed is False
    assert item.detail == "schtasks_cli_not_found"


def test_write_and_read_prelive_report(tmp_path: Path) -> None:
    checks = [
        checklist.ChecklistItem("one", True, "ok"),
        checklist.ChecklistItem("two", False, "bad"),
    ]

    path = checklist.write_pre_live_report(tmp_path, checks=checks, all_passed=False)
    assert path.exists()

    payload = checklist.read_pre_live_report(tmp_path)
    assert payload is not None
    assert payload["all_passed"] is False
    assert payload["passed_count"] == 1
    assert payload["failed_count"] == 1
