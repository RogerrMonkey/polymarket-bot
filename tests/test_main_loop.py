from __future__ import annotations

from pathlib import Path
import signal

import prediction_bot.main_loop as ml
from prediction_bot.executor import TradeRecord
from prediction_bot.models import MarketSnapshot, RiskDecision, ScanCandidate
from prediction_bot.pipeline.runner import ScanRunResult


def _fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
    snap = MarketSnapshot(
        venue="polymarket",
        market_id="m1",
        question="Will BTC close up?",
        yes_price=0.55,
        no_price=0.45,
        spread=0.01,
        volume=20000,
        liquidity=25000,
        expires_at=None,
        raw={"clobTokenIds": '["yes-token","no-token"]'},
    )
    cand = ScanCandidate(snapshot=snap, opportunity_score=1.0, anomaly_flags=[])
    decision = RiskDecision(approved=True, reasons=[], position_fraction=0.05, edge=0.1)
    return ScanRunResult(candidates=[cand], risk_decisions=[(cand, decision)], db_path="data/predictions.db")


def test_run_paper_loop_one_cycle(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ml, "execute_scan_run", _fake_run)

    code = ml.run_paper_loop(
        cycles=1,
        interval_seconds=1,
        limit_per_venue=5,
        top_n_for_risk=1,
        workspace_root=tmp_path,
        dry_run=True,
    )

    assert code == 0
    assert (tmp_path / "data" / "loop_log.jsonl").exists()
    assert (tmp_path / "data" / "trades.jsonl").exists()


def test_run_paper_loop_signal_shutdown_calls_cancel(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ml, "execute_scan_run", _fake_run)

    handlers = {}

    def _fake_signal(sig, handler):  # noqa: ANN001
        handlers[sig] = handler
        return handler

    monkeypatch.setattr(ml.signal, "signal", _fake_signal)
    monkeypatch.setattr(ml.signal, "getsignal", lambda sig: None)

    class _Executor:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            self.cancelled = False

        def place_maker_order(self, market, side, size):  # noqa: ANN001
            return TradeRecord(
                market_id="m1",
                token_id="yes-token",
                side=side,
                price=0.55,
                size_usdc=size,
                order_type="GTC",
                order_id="x",
                status="filled",
                fill_price=0.55,
                fill_size=size,
                timestamp="2026-01-01T00:00:00+00:00",
                fees_paid=0.0,
                pnl=None,
            )

        def cancel_all_open_orders(self):  # noqa: ANN201
            self.cancelled = True
            return 1

    executor_holder = {}

    def _executor_factory(*args, **kwargs):  # noqa: ANN002, ANN003
        ex = _Executor()
        executor_holder["ex"] = ex
        return ex

    monkeypatch.setattr(ml, "OrderExecutor", _executor_factory)

    sleep_calls = {"count": 0}

    def _fake_sleep(seconds):  # noqa: ANN001
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(ml.time, "sleep", _fake_sleep)

    code = ml.run_paper_loop(
        cycles=0,
        interval_seconds=3,
        limit_per_venue=5,
        top_n_for_risk=1,
        workspace_root=tmp_path,
        dry_run=True,
    )

    assert code == 130
    assert executor_holder["ex"].cancelled is True
