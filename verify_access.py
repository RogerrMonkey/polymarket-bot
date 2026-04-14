from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

CLOB_MARKETS_URL = "https://clob.polymarket.com/markets?limit=5"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?limit=5&active=true"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/"
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    latency_ms: float | None
    detail: str


def _now() -> float:
    return time.perf_counter()


def _elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def _first_market_title(payload: Any) -> str | None:
    records: list[Any] = []
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            records = payload["data"]
        elif isinstance(payload.get("results"), list):
            records = payload["results"]

    if not records:
        return None

    first = records[0]
    if not isinstance(first, dict):
        return None

    for key in ("question", "title", "market", "slug"):
        value = first.get(key)
        if value:
            return str(value)
    return None


def check_http_market(name: str, url: str) -> CheckResult:
    start = _now()
    try:
        response = requests.get(url, timeout=10)
        status_code = response.status_code
        title = None
        try:
            title = _first_market_title(response.json())
        except ValueError:
            title = None

        passed = 200 <= status_code < 300
        detail = f"status={status_code} first_market={title or 'n/a'}"
        return CheckResult(name=name, passed=passed, latency_ms=_elapsed_ms(start), detail=detail)
    except Exception as exc:  # noqa: BLE001
        detail = f"error={exc}"
        return CheckResult(name=name, passed=False, latency_ms=_elapsed_ms(start), detail=detail)


def _sample_market_asset_ids(limit: int = 5) -> list[str]:
    try:
        response = requests.get(GAMMA_MARKETS_URL, timeout=10)
        payload = response.json()
    except Exception:  # noqa: BLE001
        return []

    records: list[Any] = payload if isinstance(payload, list) else []
    for market in records[:limit]:
        if not isinstance(market, dict):
            continue

        raw_ids = market.get("clobTokenIds")
        ids: list[str] = []
        if isinstance(raw_ids, str):
            try:
                parsed = json.loads(raw_ids)
                if isinstance(parsed, list):
                    ids = [str(x) for x in parsed if x]
            except json.JSONDecodeError:
                ids = []
        elif isinstance(raw_ids, list):
            ids = [str(x) for x in raw_ids if x]

        if len(ids) >= 1:
            return ids[:2]

    return []


async def _wait_for_websocket_message(url: str, market_asset_ids: list[str]) -> tuple[bool, str]:
    import websockets

    async with websockets.connect(url, open_timeout=10, close_timeout=10) as ws:
        if url == WS_MARKET_URL and market_asset_ids:
            await ws.send(
                json.dumps(
                    {
                        "assets_ids": market_asset_ids,
                        "type": "market",
                        "custom_feature_enabled": True,
                    }
                )
            )
        else:
            await ws.send("PING")
        try:
            message = await asyncio.wait_for(ws.recv(), timeout=5)
        except asyncio.TimeoutError:
            return False, "no_message_within_5s"
        preview = str(message).replace("\n", " ").strip()
        if len(preview) > 140:
            preview = f"{preview[:140]}..."
        return True, f"message_preview={preview or 'empty'}"


def check_websocket() -> CheckResult:
    start = _now()
    try:
        market_asset_ids = _sample_market_asset_ids()
        errors: list[str] = []
        for url in (WS_URL, WS_MARKET_URL):
            try:
                passed, detail = asyncio.run(_wait_for_websocket_message(url, market_asset_ids))
                if passed:
                    final_detail = f"endpoint={url} {detail}"
                    return CheckResult(name="ws_subscription", passed=True, latency_ms=_elapsed_ms(start), detail=final_detail)
                errors.append(f"{url}:{detail}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{url}:error={exc}")

        return CheckResult(
            name="ws_subscription",
            passed=False,
            latency_ms=_elapsed_ms(start),
            detail="; ".join(errors),
        )
    except ModuleNotFoundError as exc:
        detail = f"missing_dependency={exc}"
        return CheckResult(name="ws_subscription", passed=False, latency_ms=_elapsed_ms(start), detail=detail)
    except Exception as exc:  # noqa: BLE001
        detail = f"error={exc}"
        return CheckResult(name="ws_subscription", passed=False, latency_ms=_elapsed_ms(start), detail=detail)


def resolve_clob_ip() -> str:
    host = urlparse(CLOB_MARKETS_URL).hostname
    if not host:
        return "unresolved"
    try:
        return socket.gethostbyname(host)
    except OSError as exc:
        return f"resolve_error={exc}"


def summarize_exit_code(results: list[CheckResult]) -> int:
    if not results:
        return 1
    return 0 if all(r.passed for r in results) else 1


def print_report(resolved_ip: str, results: list[CheckResult]) -> None:
    print(f"clob_resolved_ip={resolved_ip}")

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        latency = f"{result.latency_ms:.1f}ms" if result.latency_ms is not None else "n/a"
        print(f"[{status}] {result.name} latency={latency} {result.detail}")


def main() -> int:
    results = [
        check_http_market("clob_markets", CLOB_MARKETS_URL),
        check_http_market("gamma_markets", GAMMA_MARKETS_URL),
        check_websocket(),
    ]
    print_report(resolve_clob_ip(), results)
    return summarize_exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
