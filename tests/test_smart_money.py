from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from prediction_bot.research import smart_money as sm
from prediction_bot.research.smart_money import (
    SmartMoneySignal,
    TopTrader,
    TraderPosition,
    apply_smart_money_modifier,
    fetch_smart_money_signal,
    fetch_top_traders,
    fetch_trader_position,
)


# --- Fakes -------------------------------------------------------------------


class _FakeHttp:
    """Records calls + serves canned JSON keyed on url path."""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self.mapping = mapping
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url: str, params: dict | None = None):
        self.calls.append((url, params or {}))
        for key, value in self.mapping.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        return []


@dataclass
class _FakeAnalysisResult:
    """Stand-in for ClaudeAnalyst.AnalysisResult — same field shape."""
    probability: float
    decision: str
    confidence: str
    reasoning: str
    edge: float
    cost_usd: float = 0.0
    data_sources_used: list[str] | None = None
    provider: str = "test"

    def __post_init__(self):
        if self.data_sources_used is None:
            self.data_sources_used = []


# --- fetch_top_traders -------------------------------------------------------


def _trades_payload(rows: list[dict]) -> list[dict]:
    return rows


def test_fetch_top_traders_returns_ranked_list(tmp_path: Path) -> None:
    trades = [
        {"proxyWallet": "0xWHALE", "size": 100, "price": 0.50},  # $50
        {"proxyWallet": "0xWHALE", "size": 200, "price": 0.40},  # $80 — total $130
        {"proxyWallet": "0xMEDIUM", "size": 50, "price": 0.60},  # $30
        {"proxyWallet": "0xSMALL", "size": 1, "price": 0.10},    # $0.1
    ]
    http = _FakeHttp({"data-api.polymarket.com/trades": trades})

    traders = fetch_top_traders(limit=5, http=http, workspace_root=tmp_path)
    assert len(traders) == 3
    # Ranking by volume desc — whale first
    assert traders[0].address == "0xwhale"
    assert traders[0].rank == 1
    assert traders[0].pnl_usdc == 130.0  # volume proxy in this slot
    assert traders[0].total_trades == 2

    # Weight monotonic with volume
    assert traders[0].weight > traders[1].weight > traders[2].weight
    # Weight floor at 0.1
    assert traders[2].weight >= 0.1
    # Weight formula sanity
    assert math.isclose(traders[0].weight, max(math.log(1.0 + 130.0) / 10.0, 0.1), abs_tol=1e-4)


def test_fetch_top_traders_cache_hit(tmp_path: Path) -> None:
    cache_dir = tmp_path / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / "top_traders.json"
    cache.write_text(
        json.dumps(
            {
                "cached_at_unix": time.time(),  # fresh
                "ranking_method": "recent_volume_usdc",
                "traders": [
                    {
                        "address": "0xcached",
                        "rank": 1,
                        "pnl_usdc": 5000.0,
                        "total_trades": 10,
                        "win_rate": 0.0,
                        "roi_pct": 0.0,
                        "weight": 0.85,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    http = _FakeHttp({})  # would error if called
    traders = fetch_top_traders(limit=10, http=http, workspace_root=tmp_path)
    assert len(traders) == 1
    assert traders[0].address == "0xcached"
    assert http.calls == []  # zero HTTP calls on cache hit


def test_fetch_top_traders_cache_expired(tmp_path: Path) -> None:
    cache_dir = tmp_path / "data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / "top_traders.json"
    cache.write_text(
        json.dumps(
            {
                "cached_at_unix": time.time() - 7 * 3600,  # 7h old, TTL is 6h
                "traders": [
                    {"address": "0xstale", "rank": 1, "pnl_usdc": 9.9, "total_trades": 1, "win_rate": 0, "roi_pct": 0, "weight": 0.1}
                ],
            }
        ),
        encoding="utf-8",
    )

    http = _FakeHttp(
        {
            "data-api.polymarket.com/trades": [
                {"proxyWallet": "0xfresh", "size": 1000, "price": 0.5}
            ]
        }
    )
    traders = fetch_top_traders(limit=5, http=http, workspace_root=tmp_path, cache_ttl_hours=6.0)
    assert len(traders) == 1
    assert traders[0].address == "0xfresh"
    assert len(http.calls) == 1


def test_fetch_top_traders_returns_empty_on_http_failure(tmp_path: Path) -> None:
    http = _FakeHttp({"data-api.polymarket.com/trades": RuntimeError("network down")})
    traders = fetch_top_traders(limit=5, http=http, workspace_root=tmp_path)
    assert traders == []


# --- fetch_trader_position --------------------------------------------------


def test_fetch_trader_position_returns_none_below_threshold() -> None:
    http = _FakeHttp(
        {
            "data-api.polymarket.com/positions": [
                {
                    "conditionId": "0xabc",
                    "outcome": "Yes",
                    "outcomeIndex": 0,
                    "size": 50,
                    "avgPrice": 0.4,
                    "currentValue": 50.0,  # below $200 threshold
                }
            ]
        }
    )
    pos = fetch_trader_position("0xtrader", "0xabc", http=http, min_position_usdc=200.0)
    assert pos is None


def test_fetch_trader_position_returns_position_above_threshold() -> None:
    http = _FakeHttp(
        {
            "data-api.polymarket.com/positions": [
                {
                    "conditionId": "0xabc",
                    "outcome": "Yes",
                    "outcomeIndex": 0,
                    "size": 1000,
                    "avgPrice": 0.55,
                    "currentValue": 600.0,
                }
            ]
        }
    )
    pos = fetch_trader_position("0xtrader", "0xabc", http=http, min_position_usdc=200.0)
    assert pos is not None
    assert pos.side == "YES"
    assert pos.size_usdc == 600.0
    assert pos.entry_price == 0.55


def test_fetch_trader_position_dedupes_via_seen_set() -> None:
    http = _FakeHttp(
        {
            "data-api.polymarket.com/positions": [
                {"conditionId": "0xabc", "outcome": "No", "outcomeIndex": 1,
                 "size": 1000, "avgPrice": 0.45, "currentValue": 450.0}
            ]
        }
    )
    seen: set[tuple[str, str]] = set()
    p1 = fetch_trader_position("0xtrader", "0xabc", http=http, seen=seen, min_position_usdc=100.0)
    p2 = fetch_trader_position("0xtrader", "0xabc", http=http, seen=seen, min_position_usdc=100.0)
    assert p1 is not None
    assert p2 is None  # dedup hit
    assert len(http.calls) == 1


def test_fetch_trader_position_returns_none_on_error() -> None:
    http = _FakeHttp({"data-api.polymarket.com/positions": RuntimeError("timeout")})
    pos = fetch_trader_position("0xt", "0xm", http=http)
    assert pos is None


# --- fetch_smart_money_signal -----------------------------------------------


def _trader(addr: str, weight: float, rank: int = 1) -> TopTrader:
    return TopTrader(address=addr, rank=rank, pnl_usdc=1000.0, total_trades=5, win_rate=0.0, roi_pct=0.0, weight=weight)


def test_smart_money_signal_weighted_yes_prob(tmp_path: Path) -> None:
    """Weighted YES prob respects (weight * size) on each side."""
    # Build a positions endpoint that returns a different position depending on user.
    by_user = {
        "0xa": [{"conditionId": "0xmkt", "outcome": "Yes", "outcomeIndex": 0,
                 "size": 100, "avgPrice": 0.5, "currentValue": 500.0}],
        "0xb": [{"conditionId": "0xmkt", "outcome": "Yes", "outcomeIndex": 0,
                 "size": 100, "avgPrice": 0.5, "currentValue": 500.0}],
        "0xc": [{"conditionId": "0xmkt", "outcome": "No", "outcomeIndex": 1,
                 "size": 50, "avgPrice": 0.4, "currentValue": 200.0}],
    }

    class _ByUserHttp:
        def __init__(self):
            self.calls = []

        def get_json(self, url, params=None):
            self.calls.append((url, params))
            user = (params or {}).get("user", "").lower()
            return by_user.get(user, [])

    traders = [_trader("0xa", weight=1.0), _trader("0xb", weight=1.0), _trader("0xc", weight=0.5)]
    sig = fetch_smart_money_signal(
        "0xmkt",
        traders,
        http=_ByUserHttp(),
        log_path=tmp_path / "sm.jsonl",
    )
    # yes_weight = 1.0*500 + 1.0*500 = 1000;  no_weight = 0.5*200 = 100;  prob = 1000/1100 ≈ 0.909
    assert math.isclose(sig.weighted_yes_prob, 1000.0 / 1100.0, abs_tol=1e-3)
    assert sig.traders_present == 3
    assert sig.total_smart_money_usdc == 1200.0
    assert sig.largest_position_usdc == 500.0


def test_consensus_strength_thresholds() -> None:
    assert sm._consensus_strength(0, 0) == "Low"
    assert sm._consensus_strength(1, 300) == "Low"
    assert sm._consensus_strength(2, 600) == "Medium"
    assert sm._consensus_strength(3, 1200) == "High"
    # 3 traders but low total → still Medium (only one branch matches)
    assert sm._consensus_strength(3, 800) == "Medium"
    # 1 trader but $600 → Medium via OR clause
    assert sm._consensus_strength(1, 600) == "Medium"


def test_smart_money_signal_logged_to_jsonl(tmp_path: Path) -> None:
    log = tmp_path / "smart_money.jsonl"
    http = _FakeHttp(
        {
            "data-api.polymarket.com/positions": [
                {"conditionId": "0xm", "outcome": "Yes", "outcomeIndex": 0,
                 "size": 1000, "avgPrice": 0.5, "currentValue": 500.0}
            ]
        }
    )
    sig = fetch_smart_money_signal("0xm", [_trader("0xa", weight=1.0)], http=http, log_path=log)
    assert log.exists()
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["market_id"] == "0xm"
    assert rows[0]["traders_present"] == 1
    assert rows[0]["total_smart_money_usdc"] == 500.0


# --- apply_smart_money_modifier --------------------------------------------


def _signal(strength: str, yes_prob: float, traders: int = 3, total: float = 2000.0, market_id: str = "m") -> SmartMoneySignal:
    return SmartMoneySignal(
        market_id=market_id,
        traders_present=traders,
        weighted_yes_prob=yes_prob,
        consensus_strength=strength,
        largest_position_usdc=1000.0,
        recent_entries_24h=1,
        total_smart_money_usdc=total,
        top_trader_sides=["YES"] * traders,
        fetched_at="2026-05-07T00:00:00+00:00",
    )


def test_apply_smart_money_high_agree_boosts_edge() -> None:
    base = _FakeAnalysisResult(probability=0.65, decision="BUY", confidence="Medium", reasoning="model says yes", edge=0.10)
    out = apply_smart_money_modifier(base, _signal("High", yes_prob=0.70))
    assert math.isclose(out.edge, 0.12, abs_tol=1e-4)  # 0.10 * 1.20
    assert "[SM:HIGH+AGREE" in out.reasoning
    assert out.decision == "BUY"


def test_apply_smart_money_high_contradict_skips() -> None:
    base = _FakeAnalysisResult(probability=0.65, decision="BUY", confidence="High", reasoning="model says buy", edge=0.10)
    out = apply_smart_money_modifier(base, _signal("High", yes_prob=0.20))
    assert out.decision == "SKIP"
    assert "[SM:HIGH+CONTRADICT" in out.reasoning


def test_apply_smart_money_medium_agree_modest_boost() -> None:
    base = _FakeAnalysisResult(probability=0.30, decision="SELL", confidence="Medium", reasoning="model says sell", edge=0.15)
    out = apply_smart_money_modifier(base, _signal("Medium", yes_prob=0.30, traders=2, total=600.0))
    assert math.isclose(out.edge, 0.162, abs_tol=1e-4)  # 0.15 * 1.08
    assert "[SM:MED+AGREE" in out.reasoning


def test_apply_smart_money_low_no_change() -> None:
    base = _FakeAnalysisResult(probability=0.55, decision="BUY", confidence="Medium", reasoning="r", edge=0.08)
    out = apply_smart_money_modifier(base, _signal("Low", yes_prob=0.5, traders=0, total=0.0))
    assert out is base


def test_apply_smart_money_none_signal_no_change() -> None:
    base = _FakeAnalysisResult(probability=0.55, decision="BUY", confidence="Medium", reasoning="r", edge=0.08)
    out = apply_smart_money_modifier(base, None)
    assert out is base


def test_apply_smart_money_medium_contradict_no_change() -> None:
    """MEDIUM contradict is intentionally a soft signal — log only, no override."""
    base = _FakeAnalysisResult(probability=0.65, decision="BUY", confidence="Medium", reasoning="r", edge=0.10)
    out = apply_smart_money_modifier(base, _signal("Medium", yes_prob=0.30, traders=2, total=600.0))
    assert out is base
