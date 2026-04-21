"""Paper-mode P&L tracker.

Records entry/resolution pairs to `data/paper_pnl.jsonl` and provides
a summary suitable for dashboard rendering.

All amounts are in USDC. Binary-contract payoff math:
    side=BUY  & YES → +size*(1-entry_price)
    side=BUY  & NO  → -size*entry_price
    side=SELL & YES → -size*(1-entry_price)
    side=SELL & NO  → +size*entry_price

YES/NO aliases for BUY/SELL are accepted (Polymarket-native convention).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


_BUY_ALIASES = {"BUY", "YES"}
_SELL_ALIASES = {"SELL", "NO"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_side(side: str) -> str:
    """Map YES→BUY and NO→SELL. Anything unrecognised is returned as-is upper()."""
    s = (side or "").strip().upper()
    if s in _BUY_ALIASES:
        return "BUY"
    if s in _SELL_ALIASES:
        return "SELL"
    return s


def compute_pnl(side: str, entry_price: float, size_usdc: float, resolved_yes: bool) -> float:
    """Pure helper so callers (tests, dashboards) can compute P&L without instantiation."""
    side_u = _normalise_side(side)
    p = float(entry_price)
    s = float(size_usdc)
    if side_u == "BUY":
        return s * (1.0 - p) if resolved_yes else -s * p
    if side_u == "SELL":
        return -s * (1.0 - p) if resolved_yes else s * p
    return 0.0


@dataclass
class PaperPnLTracker:
    """Lightweight append-only P&L ledger.

    Each `record_entry` writes an `entry` row; each `record_resolution`
    matches by market_id (most-recent-first) and appends a `resolution`
    row that carries the computed P&L. `summary()` reduces the ledger
    into a single dashboard-ready dict.
    """

    ledger_path: Path = Path("data/paper_pnl.jsonl")

    def _append(self, payload: dict[str, Any]) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    def _read(self) -> list[dict[str, Any]]:
        if not self.ledger_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def record_entry(self, trade: dict[str, Any]) -> None:
        """Persist the opening-leg of a paper trade."""
        payload = {
            "event": "entry",
            "timestamp": trade.get("timestamp") or _utc_now_iso(),
            "market_id": str(trade.get("market_id", "")),
            "side": _normalise_side(str(trade.get("side", ""))),
            "entry_price": float(trade.get("price") or trade.get("entry_price") or 0.0),
            "size_usdc": float(trade.get("size_usdc") or trade.get("size") or 0.0),
            "order_id": trade.get("order_id"),
        }
        self._append(payload)

    def record_resolution(self, market_id: str, resolved_yes: bool) -> list[dict[str, Any]]:
        """Close all open entries for `market_id` at this resolution.

        Returns the per-trade P&L rows that were just appended (empty
        list if there were no matching open entries).
        """
        rows = self._read()
        # Collect open entries for this market (ones without a matching resolution yet).
        open_entries: list[dict[str, Any]] = []
        resolved_order_ids = {
            r.get("order_id")
            for r in rows
            if r.get("event") == "resolution" and r.get("market_id") == market_id
        }
        for r in rows:
            if r.get("event") != "entry":
                continue
            if r.get("market_id") != market_id:
                continue
            if r.get("order_id") in resolved_order_ids:
                continue
            open_entries.append(r)

        closed: list[dict[str, Any]] = []
        for entry in open_entries:
            pnl = compute_pnl(
                side=entry["side"],
                entry_price=entry["entry_price"],
                size_usdc=entry["size_usdc"],
                resolved_yes=resolved_yes,
            )
            payload = {
                "event": "resolution",
                "timestamp": _utc_now_iso(),
                "market_id": market_id,
                "order_id": entry.get("order_id"),
                "side": entry["side"],
                "entry_price": entry["entry_price"],
                "size_usdc": entry["size_usdc"],
                "resolved_yes": bool(resolved_yes),
                "pnl_usdc": round(float(pnl), 6),
            }
            self._append(payload)
            closed.append(payload)

        if closed:
            logger.info(
                "paper_pnl_resolved market={} resolved_yes={} closed={} total_pnl={}",
                market_id, resolved_yes, len(closed), round(sum(r["pnl_usdc"] for r in closed), 4),
            )
        return closed

    def summary(self) -> dict[str, Any]:
        rows = [r for r in self._read() if r.get("event") == "resolution"]
        if not rows:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "win_rate": None,
                "total_pnl_usdc": 0.0,
                "avg_pnl_per_trade": None,
                "best_trade": None,
                "worst_trade": None,
                "sharpe_approx": None,
                "awaiting_resolutions": True,
            }

        pnls = [float(r.get("pnl_usdc", 0.0)) for r in rows]
        winners = [p for p in pnls if p > 0]
        n = len(pnls)
        total_pnl = float(sum(pnls))
        avg = total_pnl / n
        best = max(pnls)
        worst = min(pnls)

        # Sharpe-approx: mean / stdev of per-trade P&L. Undefined for n<2 or zero variance.
        sharpe: float | None
        if n >= 2:
            mean = avg
            var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
            stdev = math.sqrt(var)
            sharpe = round(mean / stdev, 4) if stdev > 1e-9 else None
        else:
            sharpe = None

        return {
            "total_trades": n,
            "winning_trades": len(winners),
            "win_rate": round(len(winners) / n, 4),
            "total_pnl_usdc": round(total_pnl, 4),
            "avg_pnl_per_trade": round(avg, 4),
            "best_trade": round(best, 4),
            "worst_trade": round(worst, 4),
            "sharpe_approx": sharpe,
            "awaiting_resolutions": False,
        }
