"""Dev-only: force a synthetic paper BUY trade for a given market_id.

Bypasses the analyst and risk gate so we can smoke-test the full
resolution + P&L pipeline without waiting for Groq to approve a trade.

Usage:
    python scripts/force_paper_trade.py <market_id>

Writes a row to data/trades.jsonl with synthetic=true, forced=true and
mirrors it into data/paper_pnl.jsonl via PaperPnLTracker.record_entry.

NOT wired into the bot. Never called by main_loop or scheduler.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prediction_bot.clients.http import HttpClient  # noqa: E402
from prediction_bot.paper_pnl import PaperPnLTracker  # noqa: E402


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_market_price(market_id: str) -> float:
    """Best-effort Gamma API lookup. Falls back to 0.5 if unreachable."""
    http = HttpClient(timeout_seconds=5.0, user_agent="force_paper_trade")
    try:
        payload = http.get_json(f"https://gamma-api.polymarket.com/markets/{market_id}")
    except Exception:
        payload = None
    if isinstance(payload, dict):
        raw = payload.get("outcomePrices")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
        if isinstance(raw, list) and raw:
            try:
                return float(raw[0])
            except (TypeError, ValueError):
                pass
    print("force_paper_trade: market price unavailable; defaulting to 0.5")
    return 0.5


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/force_paper_trade.py <market_id>")
        return 2

    market_id = sys.argv[1].strip()
    price = _fetch_market_price(market_id)
    size_usdc = 1.0
    order_id = f"forced-{int(datetime.now(timezone.utc).timestamp())}"
    timestamp = _utc_now_iso()

    trade_row: dict = {
        "timestamp": timestamp,
        "market_id": market_id,
        "side": "BUY",
        "size_usdc": size_usdc,
        "price": price,
        "fill_price": price,
        "fill_size": size_usdc,
        "order_id": order_id,
        "status": "filled",
        "synthetic": True,
        "forced": True,
    }

    trades_path = ROOT / "data" / "trades.jsonl"
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    with trades_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(trade_row) + "\n")

    tracker = PaperPnLTracker(ledger_path=ROOT / "data" / "paper_pnl.jsonl")
    tracker.record_entry({
        "market_id": market_id,
        "side": "BUY",
        "price": price,
        "size_usdc": size_usdc,
        "order_id": order_id,
        "timestamp": timestamp,
    })

    print(
        f"forced_paper_trade market={market_id} side=BUY price={price:.4f} "
        f"size=${size_usdc:.2f} order_id={order_id}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
