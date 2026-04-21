from __future__ import annotations

from pathlib import Path

from prediction_bot.paper_pnl import PaperPnLTracker, compute_pnl


# --- Pure payoff math ---


def test_buy_wins_when_resolved_yes() -> None:
    # Bought YES at 0.40 with $10 size; YES resolves → win $6
    assert abs(compute_pnl("BUY", 0.40, 10.0, resolved_yes=True) - 6.0) < 1e-9


def test_buy_loses_when_resolved_no() -> None:
    assert abs(compute_pnl("BUY", 0.40, 10.0, resolved_yes=False) - (-4.0)) < 1e-9


def test_sell_wins_when_resolved_no() -> None:
    # Sold YES (= bought NO synth) at 0.40 with $10; NO resolves → win $4
    assert abs(compute_pnl("SELL", 0.40, 10.0, resolved_yes=False) - 4.0) < 1e-9


def test_sell_loses_when_resolved_yes() -> None:
    assert abs(compute_pnl("SELL", 0.40, 10.0, resolved_yes=True) - (-6.0)) < 1e-9


def test_yes_and_no_aliases_map_to_buy_and_sell() -> None:
    # YES == BUY, NO == SELL (polymarket-native convention)
    assert compute_pnl("YES", 0.40, 10.0, resolved_yes=True) == compute_pnl("BUY", 0.40, 10.0, resolved_yes=True)
    assert compute_pnl("NO", 0.40, 10.0, resolved_yes=False) == compute_pnl("SELL", 0.40, 10.0, resolved_yes=False)


# --- Tracker integration ---


def _tracker(tmp_path: Path) -> PaperPnLTracker:
    return PaperPnLTracker(ledger_path=tmp_path / "paper_pnl.jsonl")


def test_record_entry_and_resolution_round_trip(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.record_entry({
        "market_id": "m-1", "side": "BUY", "price": 0.40,
        "size_usdc": 10.0, "order_id": "ord-1",
    })
    closed = t.record_resolution("m-1", resolved_yes=True)
    assert len(closed) == 1
    assert closed[0]["pnl_usdc"] == 6.0
    assert closed[0]["order_id"] == "ord-1"


def test_resolution_without_entry_is_noop(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    closed = t.record_resolution("m-missing", resolved_yes=True)
    assert closed == []


def test_resolution_matches_only_open_entries(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.record_entry({"market_id": "m-1", "side": "BUY", "price": 0.4, "size_usdc": 10.0, "order_id": "a"})
    t.record_resolution("m-1", resolved_yes=True)  # closes 'a'
    # Second entry for same market — new position
    t.record_entry({"market_id": "m-1", "side": "BUY", "price": 0.5, "size_usdc": 5.0, "order_id": "b"})
    closed = t.record_resolution("m-1", resolved_yes=False)
    # Only 'b' should close in this second call (order_id='a' already resolved)
    assert len(closed) == 1
    assert closed[0]["order_id"] == "b"
    assert closed[0]["pnl_usdc"] == -2.5  # -5.0 * 0.5


def test_summary_empty(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    s = t.summary()
    assert s["total_trades"] == 0
    assert s["awaiting_resolutions"] is True
    assert s["win_rate"] is None


def test_summary_mixed_winners_and_losers(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    # +6 win
    t.record_entry({"market_id": "m-1", "side": "BUY", "price": 0.40, "size_usdc": 10.0, "order_id": "a"})
    t.record_resolution("m-1", resolved_yes=True)
    # -4 loss
    t.record_entry({"market_id": "m-2", "side": "BUY", "price": 0.40, "size_usdc": 10.0, "order_id": "b"})
    t.record_resolution("m-2", resolved_yes=False)
    # +4 win (SELL)
    t.record_entry({"market_id": "m-3", "side": "SELL", "price": 0.40, "size_usdc": 10.0, "order_id": "c"})
    t.record_resolution("m-3", resolved_yes=False)

    s = t.summary()
    assert s["total_trades"] == 3
    assert s["winning_trades"] == 2
    assert abs(s["win_rate"] - 2 / 3) < 1e-4
    assert abs(s["total_pnl_usdc"] - 6.0) < 1e-4
    assert s["best_trade"] == 6.0
    assert s["worst_trade"] == -4.0
    assert s["awaiting_resolutions"] is False
    # Sharpe is defined for n>=2 with non-zero variance
    assert isinstance(s["sharpe_approx"], float)


def test_summary_sharpe_none_when_single_trade(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.record_entry({"market_id": "m-1", "side": "BUY", "price": 0.40, "size_usdc": 10.0, "order_id": "a"})
    t.record_resolution("m-1", resolved_yes=True)
    s = t.summary()
    assert s["total_trades"] == 1
    assert s["sharpe_approx"] is None
