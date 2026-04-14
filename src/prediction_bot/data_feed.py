from __future__ import annotations

import asyncio
import importlib
import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

CLOB_BOOK_URL = "https://clob.polymarket.com/book"
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CHAINLINK_BTC_USD_FEED = "0xc907E116054Ad103354f2D350FD2514433D57F6f"


@dataclass
class MarketState:
    condition_id: str
    token_id_yes: str
    token_id_no: str
    price_yes: float | None
    price_no: float | None
    spread: float | None
    bid_yes: float | None
    ask_yes: float | None
    bid_no: float | None
    ask_no: float | None
    volume_24h: float | None
    last_updated: datetime
    chainlink_btc_price: float | None = None


@dataclass
class _TokenBookState:
    token_id: str
    condition_id: str
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    volume_24h: float | None = None
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_levels(raw_levels: Any, depth: int = 3) -> list[tuple[float, float]]:
    if not isinstance(raw_levels, list):
        return []

    out: list[tuple[float, float]] = []
    for level in raw_levels:
        if not isinstance(level, dict):
            continue
        price = _to_float(level.get("price"))
        size = _to_float(level.get("size") or level.get("quantity"))
        if price is None or size is None:
            continue
        out.append((price, size))
        if len(out) >= depth:
            break
    return out


def _best_bid_ask(book: _TokenBookState) -> tuple[float | None, float | None]:
    bid = book.bids[0][0] if book.bids else None
    ask = book.asks[0][0] if book.asks else None
    return bid, ask


def _build_market_state(yes: _TokenBookState, no: _TokenBookState) -> MarketState:
    bid_yes, ask_yes = _best_bid_ask(yes)
    bid_no, ask_no = _best_bid_ask(no)

    price_yes = None
    if bid_yes is not None and ask_yes is not None:
        price_yes = (bid_yes + ask_yes) / 2.0

    price_no = None
    if bid_no is not None and ask_no is not None:
        price_no = (bid_no + ask_no) / 2.0

    spread = None
    if bid_yes is not None and ask_yes is not None:
        spread = ask_yes - bid_yes

    return MarketState(
        condition_id=yes.condition_id,
        token_id_yes=yes.token_id,
        token_id_no=no.token_id,
        price_yes=price_yes,
        price_no=price_no,
        spread=spread,
        bid_yes=bid_yes,
        ask_yes=ask_yes,
        bid_no=bid_no,
        ask_no=ask_no,
        volume_24h=yes.volume_24h if yes.volume_24h is not None else no.volume_24h,
        last_updated=max(yes.last_updated, no.last_updated),
    )


class PolymarketFeed:
    def __init__(
        self,
        token_pairs: list[tuple[str, str, str]],
        on_update: Callable[[MarketState], Awaitable[None] | None] | None = None,
        websocket_url: str = WS_MARKET_URL,
        heartbeat_seconds: int = 30,
        include_chainlink_oracle: bool = True,
    ) -> None:
        self.token_pairs = token_pairs
        self.on_update = on_update
        self.websocket_url = websocket_url
        self.heartbeat_seconds = heartbeat_seconds
        self.include_chainlink_oracle = include_chainlink_oracle

        self.order_books: dict[str, _TokenBookState] = {}
        self._asset_to_condition: dict[str, str] = {}
        self._pair_by_condition: dict[str, tuple[str, str]] = {}

        for condition_id, token_yes, token_no in token_pairs:
            self._asset_to_condition[token_yes] = condition_id
            self._asset_to_condition[token_no] = condition_id
            self._pair_by_condition[condition_id] = (token_yes, token_no)
            self.order_books[token_yes] = _TokenBookState(token_id=token_yes, condition_id=condition_id)
            self.order_books[token_no] = _TokenBookState(token_id=token_no, condition_id=condition_id)

        self._last_message_at = time.monotonic()
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                await self._run_once()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = min(30.0, backoff) + random.uniform(0.0, 0.5)
                await asyncio.sleep(delay)
                backoff = min(30.0, backoff * 2.0)

    async def _run_once(self) -> None:
        import websockets

        async with websockets.connect(self.websocket_url, open_timeout=10, close_timeout=10, ping_interval=20) as ws:
            await self._subscribe(ws)
            await self._reconcile_rest_snapshot()
            self._last_message_at = time.monotonic()

            heartbeat = asyncio.create_task(self._heartbeat_monitor(ws))
            try:
                async for raw_message in ws:
                    self._last_message_at = time.monotonic()
                    await self._handle_ws_message(raw_message)
            finally:
                heartbeat.cancel()

    async def _subscribe(self, ws: Any) -> None:
        all_asset_ids: list[str] = []
        for _, yes_id, no_id in self.token_pairs:
            all_asset_ids.append(yes_id)
            all_asset_ids.append(no_id)

        payload = {
            "assets_ids": all_asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
        }
        await ws.send(json.dumps(payload))

    async def _heartbeat_monitor(self, ws: Any) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(self.heartbeat_seconds)
            if time.monotonic() - self._last_message_at > self.heartbeat_seconds:
                await ws.close()
                return

    async def _reconcile_rest_snapshot(self) -> None:
        aiohttp = importlib.import_module("aiohttp")
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for token_id in list(self.order_books.keys()):
                try:
                    async with session.get(CLOB_BOOK_URL, params={"token_id": token_id}) as response:
                        if response.status < 200 or response.status >= 300:
                            continue
                        payload = await response.json()
                        self._update_book_from_payload(token_id, payload)
                except Exception:
                    continue

    async def _handle_ws_message(self, raw_message: str) -> None:
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    await self._handle_ws_event(item)
            return

        if isinstance(payload, dict):
            await self._handle_ws_event(payload)

    async def _handle_ws_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type") or event.get("type") or "").lower()
        if event_type in {"book", "price_change", "best_bid_ask", "tick_size_change"}:
            token_id = str(event.get("asset_id") or event.get("token_id") or "")
            if not token_id:
                return
            self._update_book_from_payload(token_id, event)
            await self._emit_market_state_if_ready(token_id)

    def _update_book_from_payload(self, token_id: str, payload: dict[str, Any]) -> None:
        book = self.order_books.get(token_id)
        if book is None:
            condition_id = self._asset_to_condition.get(token_id, "")
            if not condition_id:
                return
            book = _TokenBookState(token_id=token_id, condition_id=condition_id)
            self.order_books[token_id] = book

        bids = payload.get("bids") or payload.get("buy") or payload.get("buys")
        asks = payload.get("asks") or payload.get("sell") or payload.get("sells")

        if bids is not None:
            book.bids = _parse_levels(bids)
        if asks is not None:
            book.asks = _parse_levels(asks)

        volume = _to_float(payload.get("volume_24h") or payload.get("volume") or payload.get("volumeNum"))
        if volume is not None:
            book.volume_24h = volume

        book.last_updated = _utc_now()

    async def _emit_market_state_if_ready(self, token_id: str) -> None:
        condition_id = self._asset_to_condition.get(token_id)
        if not condition_id:
            return

        pair = self._pair_by_condition.get(condition_id)
        if pair is None:
            return
        yes_id, no_id = pair

        yes_book = self.order_books.get(yes_id)
        no_book = self.order_books.get(no_id)
        if yes_book is None or no_book is None:
            return

        if not yes_book.bids or not yes_book.asks:
            return
        if not no_book.bids or not no_book.asks:
            return

        state = _build_market_state(yes_book, no_book)
        if self.include_chainlink_oracle:
            try:
                state.chainlink_btc_price = get_chainlink_btc_price()
            except Exception:
                state.chainlink_btc_price = None
        if self.on_update is None:
            return

        maybe_coro = self.on_update(state)
        if asyncio.iscoroutine(maybe_coro):
            await maybe_coro


_CHAINLINK_CACHE: dict[str, float | None] = {"value": None}
_CHAINLINK_CACHE_TS = 0.0


def _read_chainlink_btc_price(rpc_url: str) -> float:
    web3_module = importlib.import_module("web3")
    Web3 = getattr(web3_module, "Web3")

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))

    abi = [
        {
            "inputs": [],
            "name": "latestRoundData",
            "outputs": [
                {"internalType": "uint80", "name": "roundId", "type": "uint80"},
                {"internalType": "int256", "name": "answer", "type": "int256"},
                {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
                {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
                {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
            ],
            "stateMutability": "view",
            "type": "function",
        },
        {
            "inputs": [],
            "name": "decimals",
            "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
            "stateMutability": "view",
            "type": "function",
        },
    ]

    contract = w3.eth.contract(address=Web3.to_checksum_address(CHAINLINK_BTC_USD_FEED), abi=abi)
    round_data = contract.functions.latestRoundData().call()
    raw_answer = int(round_data[1])
    decimals = int(contract.functions.decimals().call())
    return raw_answer / float(10**decimals)


def get_chainlink_btc_price() -> float | None:
    global _CHAINLINK_CACHE_TS

    now = time.monotonic()
    if now - _CHAINLINK_CACHE_TS < 5 and _CHAINLINK_CACHE["value"] is not None:
        return _CHAINLINK_CACHE["value"]

    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    try:
        value = _read_chainlink_btc_price(rpc_url)
    except Exception:
        return _CHAINLINK_CACHE["value"]

    _CHAINLINK_CACHE["value"] = value
    _CHAINLINK_CACHE_TS = now
    return value
