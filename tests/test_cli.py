from __future__ import annotations

from pathlib import Path

import prediction_bot.cli as cli
from prediction_bot.outcome_resolver import ResolutionRunReport
from prediction_bot.synthetic_replay import SyntheticReplayReport


def test_run_loop_command_blocks_live_without_checklist(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    class _Cfg:
        class storage:
            db_path = "data/predictions.db"

    monkeypatch.setattr(cli, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cli, "run_pre_live_checklist", lambda workspace_root, db_path: (False, []))
    monkeypatch.setattr(cli, "print_pre_live_report", lambda checks, all_passed: None)
    monkeypatch.setattr(cli, "write_pre_live_report", lambda workspace_root, checks, all_passed: Path("x"))

    called = {"ran": False}

    def _run_loop(**kwargs):  # noqa: ANN003
        called["ran"] = True
        return 0

    monkeypatch.setattr(cli, "run_paper_loop", _run_loop)

    code = cli.run_loop_command(
        cycles=1,
        interval_seconds=1,
        limit_per_venue=1,
        top_n_for_risk=1,
        dry_run=False,
        allow_live_without_checklist=False,
    )

    assert code == 1
    assert called["ran"] is False


def test_run_loop_command_allows_live_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    class _Cfg:
        class storage:
            db_path = "data/predictions.db"

    monkeypatch.setattr(cli, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cli, "run_pre_live_checklist", lambda workspace_root, db_path: (False, []))
    monkeypatch.setattr(cli, "print_pre_live_report", lambda checks, all_passed: None)
    monkeypatch.setattr(cli, "write_pre_live_report", lambda workspace_root, checks, all_passed: Path("x"))

    monkeypatch.setattr(cli, "run_paper_loop", lambda **kwargs: 0)

    code = cli.run_loop_command(
        cycles=1,
        interval_seconds=1,
        limit_per_venue=1,
        top_n_for_risk=1,
        dry_run=False,
        allow_live_without_checklist=True,
    )

    assert code == 0


def test_run_synthetic_replay_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    class _Cfg:
        class storage:
            db_path = "data/predictions.db"

    monkeypatch.setattr(cli, "load_config", lambda: _Cfg())
    monkeypatch.setattr(
        cli,
        "run_synthetic_replay",
        lambda **kwargs: SyntheticReplayReport(
            scenario="default",
            days=1,
            loops_written=1,
            predictions_written=1,
            approved_signals=1,
            trades_written=1,
            risk_events_written=0,
            analyses_written=1,
            outcomes_written=1,
            unresolved_stub_entries=0,
        ),
    )
    monkeypatch.setattr(cli, "print_synthetic_replay_report", lambda report: None)

    code = cli.run_synthetic_replay_command(
        days=1,
        loops_per_day=1,
        candidates_per_loop=1,
        approve_rate=0.5,
        resolved_rate=0.5,
        scenario="default",
        seed=1,
    )

    assert code == 0


def test_run_resolve_outcomes_command(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    class _Cfg:
        class storage:
            db_path = "data/predictions.db"

        class runtime:
            request_timeout_seconds = 5.0
            user_agent = "test-agent"

    class _Resolver:
        def __init__(self, **kwargs):  # noqa: ANN003
            pass

        def settle_unresolved_predictions(self, store, limit, force_all=False):  # noqa: ANN001
            return ResolutionRunReport(checked=1, resolved=1, applied=0, unresolved=0, errors=0, updates=[])

    monkeypatch.setattr(cli, "load_config", lambda: _Cfg())
    monkeypatch.setattr(cli, "OutcomeResolver", _Resolver)
    monkeypatch.setattr(cli, "print_resolution_report", lambda report: None)

    code = cli.run_resolve_outcomes_command(limit=10, dry_run=True, stub_mode=True, stub_path=None)
    assert code == 0
