from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prediction_bot.models import MarketSnapshot


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_price(value: float) -> float:
    return max(0.01, min(0.99, value))


@dataclass
class TradeRecord:
    market_id: str
    token_id: str
    side: str
    price: float
    size_usdc: float
    order_type: str
    order_id: str
    status: str
    fill_price: float | None
    fill_size: float | None
    timestamp: str
    fees_paid: float
    pnl: float | None


class OrderExecutor:
    def __init__(
        self,
        clob_client: Any | None = None,
        dry_run: bool | None = None,
        trades_path: str | Path = "data/trades.jsonl",
    ) -> None:
        self.client = clob_client
        self.dry_run = self._resolve_dry_run(dry_run)
        self.live_mode = self._resolve_live_mode()
        self.live_stub = self._resolve_live_stub_mode(self.live_mode)
        self.poll_seconds = self._resolve_poll_seconds()
        self.trades_path = Path(trades_path)
        self._open_order_ids: set[str] = set()

    @staticmethod
    def _resolve_dry_run(dry_run: bool | None) -> bool:
        if dry_run is not None:
            return dry_run
        raw = os.getenv("DRY_RUN", "true").strip().lower()
        return raw in {"1", "true", "yes", "on"}

    @staticmethod
    def _to_bool(value: str | None, default: bool) -> bool:
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    @classmethod
    def _resolve_live_mode(cls) -> bool:
        # Explicit switch used for future production activation.
        return cls._to_bool(os.getenv("EXECUTOR_LIVE_MODE"), default=False)

    @classmethod
    def _resolve_live_stub_mode(cls, live_mode: bool) -> bool:
        override = os.getenv("BOT_EXECUTOR_LIVE_STUB")
        if override is not None and override.strip() != "":
            return cls._to_bool(override, default=True)

        # Safe default: if live mode is not explicitly enabled, keep stub mode on.
        return not live_mode

    @staticmethod
    def _resolve_poll_seconds() -> int:
        raw = os.getenv("BOT_EXECUTOR_ORDER_POLL_SECONDS", "30").strip()
        try:
            return max(5, int(raw))
        except ValueError:
            return 30

    def _append_trade(self, record: TradeRecord) -> None:
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        with self.trades_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record)) + "\n")

        # Mirror only newly-filled opening legs into the paper P&L ledger so
        # we get clean entry rows (one per order). Cancels, failures, and
        # status-update rewrites do not belong in the P&L tracker.
        try:
            if getattr(record, "status", None) == "filled" and getattr(record, "fill_size", 0) and getattr(record, "fill_price", None) is not None:
                from prediction_bot.paper_pnl import PaperPnLTracker  # local import

                PaperPnLTracker(ledger_path=self.trades_path.parent / "paper_pnl.jsonl").record_entry({
                    "market_id": record.market_id,
                    "side": record.side,
                    "price": record.fill_price,
                    "size_usdc": record.fill_size,
                    "order_id": record.order_id,
                    "timestamp": record.timestamp,
                })
        except Exception:
            # Never let P&L bookkeeping break the order path.
            pass

    def _warn_live_stub_skip(self, order_type: str, market_id: str) -> None:
        print(
            "executor_live_stub_warning="
            f"skipping_real_{order_type.lower()}_order market_id={market_id} "
            f"EXECUTOR_LIVE_MODE={self.live_mode} BOT_EXECUTOR_LIVE_STUB={self.live_stub}"
        )

    def _new_record(
        self,
        market: MarketSnapshot,
        token_id: str,
        side: str,
        price: float,
        size_usdc: float,
        order_type: str,
        order_id: str,
        status: str,
        fill_price: float | None = None,
        fill_size: float | None = None,
    ) -> TradeRecord:
        return TradeRecord(
            market_id=market.market_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=round(size_usdc, 6),
            order_type=order_type,
            order_id=order_id,
            status=status,
            fill_price=fill_price,
            fill_size=fill_size,
            timestamp=_utc_now_iso(),
            fees_paid=0.0,
            pnl=None,
        )

    def _extract_order_id(self, payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            for key in ("orderID", "order_id", "id"):
                value = payload.get(key)
                if value:
                    return str(value)
            order = payload.get("order")
            if isinstance(order, dict):
                for key in ("orderID", "order_id", "id"):
                    value = order.get(key)
                    if value:
                        return str(value)
        return fallback

    def _extract_status(self, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in ("status", "state"):
                value = payload.get(key)
                if value:
                    return str(value).lower()
            order = payload.get("order")
            if isinstance(order, dict):
                for key in ("status", "state"):
                    value = order.get(key)
                    if value:
                        return str(value).lower()
        return None

    def _extract_fill_price(self, payload: Any, fallback: float) -> float:
        if isinstance(payload, dict):
            for key in ("avg_price", "fill_price", "price"):
                value = payload.get(key)
                try:
                    if value is not None:
                        return float(value)
                except (TypeError, ValueError):
                    continue
        return fallback

    def _extract_fill_size(self, payload: Any, fallback: float) -> float:
        if isinstance(payload, dict):
            for key in ("filled_size", "size", "quantity"):
                value = payload.get(key)
                try:
                    if value is not None:
                        return float(value)
                except (TypeError, ValueError):
                    continue
        return fallback

    def _submit_live_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_usdc: float,
        order_type: str,
    ) -> tuple[str, Any]:
        if self.client is None:
            raise RuntimeError("missing_live_client")

        payload = {
            "token_id": token_id,
            "side": side,
            "price": round(price, 6),
            "size": round(size_usdc, 6),
            "order_type": order_type,
        }

        response: Any = None
        fallback_order_id = str(uuid.uuid4())

        # Common py-clob-client style path.
        if hasattr(self.client, "create_order") and hasattr(self.client, "post_order"):
            created = self.client.create_order(payload)
            response = self.client.post_order(created)
        elif hasattr(self.client, "post_order"):
            response = self.client.post_order(payload)
        else:
            raise RuntimeError("client_missing_post_order")

        order_id = self._extract_order_id(response, fallback=fallback_order_id)
        return order_id, response

    def _poll_order_until_terminal(self, order_id: str, timeout_seconds: int) -> tuple[str, Any | None]:
        if self.client is None or not hasattr(self.client, "get_order"):
            return "unknown", None

        deadline = time.monotonic() + max(1, timeout_seconds)
        last_payload: Any = None

        while time.monotonic() < deadline:
            try:
                payload = self.client.get_order(order_id)
                last_payload = payload
            except Exception:  # noqa: BLE001
                time.sleep(1)
                continue

            status = (self._extract_status(payload) or "").lower()
            if status in {"filled", "cancelled", "canceled", "rejected", "failed"}:
                if status == "canceled":
                    status = "cancelled"
                return status, payload

            time.sleep(1)

        return "timeout", last_payload

    def _cancel_order(self, order_id: str) -> None:
        if self.client is None:
            return
        if not hasattr(self.client, "cancel_order"):
            return
        self.client.cancel_order(order_id)

    def _submit_stub_live_lifecycle(
        self,
        market: MarketSnapshot,
        token_id: str,
        side: str,
        price: float,
        size_usdc: float,
        order_type: str,
    ) -> TradeRecord:
        self._warn_live_stub_skip(order_type=order_type, market_id=market.market_id)
        order_id = f"stub-{uuid.uuid4()}"

        pending = self._new_record(
            market=market,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=size_usdc,
            order_type=order_type,
            order_id=order_id,
            status="pending",
        )
        self._append_trade(pending)

        final = self._new_record(
            market=market,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=size_usdc,
            order_type=order_type,
            order_id=order_id,
            status="filled",
            fill_price=price,
            fill_size=size_usdc,
        )
        self._append_trade(final)
        return final

    @staticmethod
    def _market_token_id(market: MarketSnapshot, side: str) -> str:
        raw = market.raw if isinstance(market.raw, dict) else {}
        ids: list[str] = []
        raw_ids = raw.get("clobTokenIds")
        if isinstance(raw_ids, str):
            try:
                parsed = json.loads(raw_ids)
                if isinstance(parsed, list):
                    ids = [str(x) for x in parsed if x]
            except json.JSONDecodeError:
                ids = []
        elif isinstance(raw_ids, list):
            ids = [str(x) for x in raw_ids if x]

        if len(ids) >= 2:
            return ids[0] if side == "YES" else ids[1]
        return "unknown-token"

    @staticmethod
    def _maker_price(market: MarketSnapshot, side: str) -> float:
        spread = market.spread or 0.0
        base_yes = market.yes_price if market.yes_price is not None else 0.5
        base_no = market.no_price if market.no_price is not None else (1.0 - base_yes)

        bid_yes = _clamp_price(base_yes - (spread / 2.0))
        bid_no = _clamp_price(base_no - (spread / 2.0))

        if side == "YES":
            return _clamp_price(bid_yes + 0.01)
        return _clamp_price(bid_no + 0.01)

    def place_maker_order(self, market: MarketSnapshot, decision: str, size_usdc: float) -> TradeRecord:
        side = decision.upper()
        if side not in {"YES", "NO"}:
            raise ValueError("decision must be YES or NO")

        maker_price = self._maker_price(market, side)
        token_id = self._market_token_id(market, side)
        order_id = f"dry-{uuid.uuid4()}" if self.dry_run else str(uuid.uuid4())

        if self.dry_run:
            record = TradeRecord(
                market_id=market.market_id,
                token_id=token_id,
                side=side,
                price=maker_price,
                size_usdc=round(size_usdc, 6),
                order_type="GTC",
                order_id=order_id,
                status="filled",
                fill_price=maker_price,
                fill_size=round(size_usdc, 6),
                timestamp=_utc_now_iso(),
                fees_paid=0.0,
                pnl=None,
            )
            self._append_trade(record)
            return record

        if self.live_stub:
            return self._submit_stub_live_lifecycle(
                market=market,
                token_id=token_id,
                side=side,
                price=maker_price,
                size_usdc=size_usdc,
                order_type="GTC",
            )

        if self.client is None:
            record = TradeRecord(
                market_id=market.market_id,
                token_id=token_id,
                side=side,
                price=maker_price,
                size_usdc=round(size_usdc, 6),
                order_type="GTC",
                order_id=order_id,
                status="failed",
                fill_price=None,
                fill_size=None,
                timestamp=_utc_now_iso(),
                fees_paid=0.0,
                pnl=None,
            )
            self._append_trade(record)
            return record

        try:
            order_id, _ = self._submit_live_order(
                token_id=token_id,
                side=side,
                price=maker_price,
                size_usdc=size_usdc,
                order_type="GTC",
            )
        except Exception:  # noqa: BLE001
            failed = self._new_record(
                market=market,
                token_id=token_id,
                side=side,
                price=maker_price,
                size_usdc=size_usdc,
                order_type="GTC",
                order_id=order_id,
                status="failed",
            )
            self._append_trade(failed)
            return failed

        pending = self._new_record(
            market=market,
            token_id=token_id,
            side=side,
            price=maker_price,
            size_usdc=size_usdc,
            order_type="GTC",
            order_id=order_id,
            status="pending",
        )
        self._append_trade(pending)
        self._open_order_ids.add(order_id)

        status, payload = self._poll_order_until_terminal(order_id, timeout_seconds=self.poll_seconds)
        if status == "filled":
            self._open_order_ids.discard(order_id)
            filled = self._new_record(
                market=market,
                token_id=token_id,
                side=side,
                price=maker_price,
                size_usdc=size_usdc,
                order_type="GTC",
                order_id=order_id,
                status="filled",
                fill_price=self._extract_fill_price(payload, fallback=maker_price),
                fill_size=self._extract_fill_size(payload, fallback=size_usdc),
            )
            self._append_trade(filled)
            return filled

        if status == "timeout":
            try:
                self._cancel_order(order_id)
            except Exception:  # noqa: BLE001
                pass
            status = "cancelled"

        self._open_order_ids.discard(order_id)
        ended = self._new_record(
            market=market,
            token_id=token_id,
            side=side,
            price=maker_price,
            size_usdc=size_usdc,
            order_type="GTC",
            order_id=order_id,
            status=status,
        )
        self._append_trade(ended)
        return ended

    def place_taker_order(
        self,
        market: MarketSnapshot,
        decision: str,
        size_usdc: float,
        is_latency_arb: bool = False,
    ) -> TradeRecord:
        side = decision.upper()
        if side not in {"YES", "NO"}:
            raise ValueError("decision must be YES or NO")

        token_id = self._market_token_id(market, side)
        order_id = f"dry-{uuid.uuid4()}" if self.dry_run else str(uuid.uuid4())

        if not is_latency_arb:
            record = TradeRecord(
                market_id=market.market_id,
                token_id=token_id,
                side=side,
                price=0.0,
                size_usdc=round(size_usdc, 6),
                order_type="FOK",
                order_id=order_id,
                status="failed",
                fill_price=None,
                fill_size=None,
                timestamp=_utc_now_iso(),
                fees_paid=0.0,
                pnl=None,
            )
            self._append_trade(record)
            return record

        if (market.spread or 0.0) > 0.03:
            record = TradeRecord(
                market_id=market.market_id,
                token_id=token_id,
                side=side,
                price=0.0,
                size_usdc=round(size_usdc, 6),
                order_type="FOK",
                order_id=order_id,
                status="failed",
                fill_price=None,
                fill_size=None,
                timestamp=_utc_now_iso(),
                fees_paid=0.0,
                pnl=None,
            )
            self._append_trade(record)
            return record

        ask_yes = _clamp_price((market.yes_price or 0.5) + ((market.spread or 0.0) / 2.0))
        ask_no = _clamp_price((market.no_price or 0.5) + ((market.spread or 0.0) / 2.0))
        taker_price = ask_yes if side == "YES" else ask_no

        if not self.dry_run and self.live_stub:
            return self._submit_stub_live_lifecycle(
                market=market,
                token_id=token_id,
                side=side,
                price=taker_price,
                size_usdc=size_usdc,
                order_type="FOK",
            )

        record = TradeRecord(
            market_id=market.market_id,
            token_id=token_id,
            side=side,
            price=taker_price,
            size_usdc=round(size_usdc, 6),
            order_type="FOK",
            order_id=order_id,
            status="filled" if self.dry_run else "pending",
            fill_price=taker_price if self.dry_run else None,
            fill_size=round(size_usdc, 6) if self.dry_run else None,
            timestamp=_utc_now_iso(),
            fees_paid=0.0,
            pnl=None,
        )
        if not self.dry_run:
            self._open_order_ids.add(order_id)
        self._append_trade(record)
        return record

    def cancel_all_open_orders(self) -> int:
        if self.dry_run:
            count = len(self._open_order_ids)
            self._open_order_ids.clear()
            return count

        if self.client is None:
            return 0

        cancelled = 0
        for order_id in list(self._open_order_ids):
            try:
                self.client.cancel_order(order_id)
                cancelled += 1
                self._open_order_ids.discard(order_id)
            except Exception:  # noqa: BLE001
                continue
        return cancelled
