from __future__ import annotations

from pathlib import Path

from prediction_bot.executor import OrderExecutor
from prediction_bot.models import MarketSnapshot


def _market(spread: float = 0.01) -> MarketSnapshot:
    return MarketSnapshot(
        venue="polymarket",
        market_id="m1",
        question="Will BTC close up?",
        yes_price=0.55,
        no_price=0.45,
        spread=spread,
        volume=20000,
        liquidity=25000,
        expires_at=None,
        raw={"clobTokenIds": '["yes-token","no-token"]'},
    )


def test_place_maker_order_dry_run(tmp_path: Path) -> None:
    ex = OrderExecutor(dry_run=True, trades_path=tmp_path / "trades.jsonl")
    record = ex.place_maker_order(_market(), "YES", 12.5)
    assert record.status == "filled"
    assert record.side == "YES"
    assert (tmp_path / "trades.jsonl").exists()


def test_place_taker_order_rejects_wide_spread(tmp_path: Path) -> None:
    ex = OrderExecutor(dry_run=True, trades_path=tmp_path / "trades.jsonl")
    record = ex.place_taker_order(_market(spread=0.05), "NO", 10.0, is_latency_arb=True)
    assert record.status == "failed"


def test_place_taker_requires_latency_flag(tmp_path: Path) -> None:
    ex = OrderExecutor(dry_run=True, trades_path=tmp_path / "trades.jsonl")
    record = ex.place_taker_order(_market(), "YES", 10.0, is_latency_arb=False)
    assert record.status == "failed"


def test_place_maker_order_live_stub_mode(tmp_path: Path) -> None:
    ex = OrderExecutor(dry_run=False, clob_client=None, trades_path=tmp_path / "trades.jsonl")
    record = ex.place_maker_order(_market(), "YES", 8.0)

    assert record.status == "filled"
    lines = (tmp_path / "trades.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 2
