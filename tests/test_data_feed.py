from __future__ import annotations

from datetime import datetime, timezone
import asyncio

import pytest

from prediction_bot.data_feed import (
    PolymarketFeed,
    _TokenBookState,
    _build_market_state,
    _parse_levels,
    get_chainlink_btc_price,
)
import prediction_bot.data_feed as data_feed


def test_parse_levels_limits_and_filters() -> None:
    raw = [
        {"price": "0.55", "size": "100"},
        {"price": "bad", "size": "50"},
        {"price": "0.56", "quantity": "80"},
        {"price": "0.57", "size": "70"},
    ]

    levels = _parse_levels(raw, depth=2)
    assert levels == [(0.55, 100.0), (0.56, 80.0)]


def test_build_market_state() -> None:
    now = datetime.now(timezone.utc)
    yes = _TokenBookState(
        token_id="yes",
        condition_id="cond",
        bids=[(0.52, 10.0)],
        asks=[(0.54, 10.0)],
        volume_24h=15000.0,
        last_updated=now,
    )
    no = _TokenBookState(
        token_id="no",
        condition_id="cond",
        bids=[(0.46, 12.0)],
        asks=[(0.48, 12.0)],
        volume_24h=15000.0,
        last_updated=now,
    )

    state = _build_market_state(yes, no)
    assert state.condition_id == "cond"
    assert state.price_yes == 0.53
    assert state.price_no == 0.47
    assert state.spread == pytest.approx(0.02)


def test_get_chainlink_btc_price_uses_cache(monkeypatch) -> None:
    monkeypatch.setenv("POLYGON_RPC_URL", "https://example-rpc")

    calls = {"count": 0}

    def _fake_read(rpc_url: str) -> float:  # noqa: ARG001
        calls["count"] += 1
        return 70000.0

    monkeypatch.setattr(data_feed, "_read_chainlink_btc_price", _fake_read)
    monkeypatch.setattr(data_feed, "_CHAINLINK_CACHE", {"value": None})
    monkeypatch.setattr(data_feed, "_CHAINLINK_CACHE_TS", 0.0)

    first = get_chainlink_btc_price()
    second = get_chainlink_btc_price()

    assert first == 70000.0
    assert second == 70000.0
    assert calls["count"] == 1


def test_feed_emits_chainlink_price(monkeypatch) -> None:
    captured = {"value": None}

    async def _on_update(state):  # noqa: ANN001
        captured["value"] = state.chainlink_btc_price

    feed = PolymarketFeed(
        token_pairs=[("cond-1", "yes-1", "no-1")],
        on_update=_on_update,
        include_chainlink_oracle=True,
    )

    yes = feed.order_books["yes-1"]
    no = feed.order_books["no-1"]
    yes.bids = [(0.51, 10.0)]
    yes.asks = [(0.53, 10.0)]
    no.bids = [(0.47, 10.0)]
    no.asks = [(0.49, 10.0)]

    monkeypatch.setattr(data_feed, "get_chainlink_btc_price", lambda: 71234.5)
    asyncio.run(feed._emit_market_state_if_ready("yes-1"))

    assert captured["value"] == 71234.5
