from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from prediction_bot.clients.http import HttpClient
from prediction_bot.storage.prediction_store import PredictionStore


def resolved_markets_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "resolved_markets.jsonl"


def _read_resolved_market_ids(workspace_root: Path) -> set[str]:
    path = resolved_markets_path(workspace_root)
    if not path.exists():
        return set()
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = row.get("market_id")
        if mid:
            out.add(str(mid))
    return out


def _read_trade_entry(workspace_root: Path, market_id: str) -> dict[str, Any] | None:
    """Return the most recent filled trade entry for market_id, or None."""
    path = workspace_root / "data" / "trades.jsonl"
    if not path.exists():
        return None
    latest: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("market_id") or "") != str(market_id):
            continue
        latest = row
    return latest


def _resolved_market_count(workspace_root: Path) -> int:
    return len(_read_resolved_market_ids(workspace_root))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolver_report_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "resolver_last_report.json"


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_outcomes(raw: Any) -> list[str]:
    if raw is None:
        return []
    parsed: Any = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed]


def _parse_outcome_prices(raw: Any) -> list[float]:
    if raw is None:
        return []
    parsed: Any = raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []

    out: list[float] = []
    for item in parsed:
        value = _as_float(item)
        if value is None:
            continue
        out.append(value)
    return out


def _normalized_outcome(value: Any) -> float | None:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"yes", "y", "true", "1"}:
            return 1.0
        if normalized in {"no", "n", "false", "0"}:
            return 0.0

    numeric = _as_float(value)
    if numeric in {0.0, 1.0}:
        return numeric
    return None


@dataclass(frozen=True)
class ResolutionDecision:
    resolved: bool
    outcome: float | None
    source: str
    detail: str


@dataclass(frozen=True)
class ResolutionUpdate:
    prediction_id: int
    market_id: str
    outcome: float
    source: str
    applied: bool


@dataclass(frozen=True)
class ResolutionRunReport:
    checked: int
    resolved: int
    applied: int
    unresolved: int
    errors: int
    updates: list[ResolutionUpdate]


class OutcomeResolver:
    def __init__(
        self,
        workspace_root: Path,
        http: HttpClient | None = None,
        dry_run: bool = True,
        stub_mode: bool = True,
        stub_path: Path | None = None,
    ) -> None:
        self.workspace_root = workspace_root
        self.http = http
        self.dry_run = dry_run
        self.stub_mode = stub_mode
        self.stub_path = stub_path or (workspace_root / "data" / "resolution_stub.json")
        env_stub_path = os.getenv("BOT_RESOLVER_STUB_PATH", "").strip()
        if env_stub_path:
            self.stub_path = Path(env_stub_path)

        self._stub_map = self._read_stub_map()

    def _read_stub_map(self) -> dict[str, float]:
        if not self.stub_path.exists():
            return {}
        try:
            data = json.loads(self.stub_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}

        out: dict[str, float] = {}
        for key, value in data.items():
            normalized = _normalized_outcome(value)
            if normalized is None:
                continue
            out[str(key)] = normalized
        return out

    def _market_payload(self, market_id: str) -> dict[str, Any] | None:
        if self.http is None:
            return None

        base = "https://gamma-api.polymarket.com"
        delays = (1.0, 2.0, 4.0)  # 3 retries, exponential backoff

        for attempt, delay in enumerate((0.0,) + delays):
            if delay > 0:
                time.sleep(delay)
            try:
                payload = self.http.get_json(f"{base}/markets/{market_id}")
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "resolver_gamma_retry market={} attempt={} endpoint=/markets/id error={}",
                    market_id, attempt + 1, exc,
                )

            try:
                payload = self.http.get_json(f"{base}/markets", params={"id": market_id, "limit": 1})
                if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                    return payload[0]
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "resolver_gamma_retry market={} attempt={} endpoint=/markets?id error={}",
                    market_id, attempt + 1, exc,
                )

        logger.warning("resolver_gamma_unavailable market={} after_retries=3", market_id)
        return None

    def resolve_market_outcome(self, market_id: str) -> ResolutionDecision:
        if self.stub_mode:
            stub_outcome = self._stub_map.get(market_id)
            if stub_outcome in {0.0, 1.0}:
                return ResolutionDecision(
                    resolved=True,
                    outcome=stub_outcome,
                    source="stub",
                    detail="resolution_stub_map",
                )

        payload = self._market_payload(market_id)
        if payload is None:
            return ResolutionDecision(
                resolved=False,
                outcome=None,
                source="api",
                detail="market_payload_unavailable",
            )

        winner = payload.get("winner") or payload.get("result") or payload.get("resolution")
        winner_outcome = _normalized_outcome(winner)
        if winner_outcome in {0.0, 1.0}:
            return ResolutionDecision(
                resolved=True,
                outcome=winner_outcome,
                source="api",
                detail="winner_field",
            )

        resolved_flag = bool(payload.get("isResolved") or payload.get("resolved"))
        closed_flag = bool(payload.get("closed"))
        if closed_flag and not resolved_flag:
            return ResolutionDecision(
                resolved=False,
                outcome=None,
                source="api",
                detail="closed_not_settled",
            )

        outcomes = _parse_outcomes(payload.get("outcomes"))
        prices = _parse_outcome_prices(payload.get("outcomePrices"))

        if resolved_flag and len(prices) >= 2:
            lowered = [x.lower() for x in outcomes]
            yes_idx = 0
            no_idx = 1
            if "yes" in lowered and "no" in lowered:
                yes_idx = lowered.index("yes")
                no_idx = lowered.index("no")

            yes_price = prices[yes_idx] if yes_idx < len(prices) else None
            no_price = prices[no_idx] if no_idx < len(prices) else None
            if yes_price is not None and no_price is not None:
                if yes_price >= 0.99:
                    return ResolutionDecision(True, 1.0, "api", "outcome_prices_yes")
                if no_price >= 0.99:
                    return ResolutionDecision(True, 0.0, "api", "outcome_prices_no")
                if yes_price > no_price:
                    return ResolutionDecision(True, 1.0, "api", "outcome_prices_max")
                if no_price > yes_price:
                    return ResolutionDecision(True, 0.0, "api", "outcome_prices_max")

        return ResolutionDecision(
            resolved=False,
            outcome=None,
            source="api",
            detail="market_not_resolved",
        )

    def settle_unresolved_predictions(self, store: PredictionStore, limit: int = 200) -> ResolutionRunReport:
        updates: list[ResolutionUpdate] = []
        errors = 0

        already_resolved = _read_resolved_market_ids(self.workspace_root)
        rows = store.unresolved_predictions(limit=limit)
        checked = len(rows)

        for row in rows:
            prediction_id = int(row["id"])
            market_id = str(row["market_id"])

            # Skip markets already resolved in resolved_markets.jsonl to avoid
            # redundant Gamma API calls across daily runs.
            if market_id in already_resolved:
                continue

            try:
                decision = self.resolve_market_outcome(market_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("resolver_exception market={} error={}", market_id, exc)
                errors += 1
                continue

            if not decision.resolved or decision.outcome not in {0.0, 1.0}:
                # Helpful diagnostic: end_date has passed but Gamma says still unresolved.
                if decision.detail in {"market_not_resolved", "closed_not_settled"}:
                    logger.info(
                        "resolver_pending market={} detail={} — will retry next run",
                        market_id, decision.detail,
                    )
                continue

            applied = False
            if not self.dry_run:
                applied = store.set_outcome(prediction_id=prediction_id, outcome=float(decision.outcome))
                # Close any matching paper-P&L entries for this market.
                # Lazy import keeps outcome_resolver importable even if paper_pnl is stripped.
                try:
                    from prediction_bot.paper_pnl import PaperPnLTracker  # local import

                    PaperPnLTracker(
                        ledger_path=self.workspace_root / "data" / "paper_pnl.jsonl"
                    ).record_resolution(
                        market_id=market_id,
                        resolved_yes=bool(float(decision.outcome) >= 0.5),
                    )
                except Exception as exc:  # noqa: BLE001
                    # P&L tracking must never block the resolver.
                    logger.warning("paper_pnl_resolution_failed market={} error={}", market_id, exc)

                # Append to resolved_markets.jsonl for skip-list on future runs.
                try:
                    # row is sqlite3.Row (indexable, no .get); guard the lookup.
                    try:
                        question = str(row["question"] or "")
                    except (IndexError, KeyError):
                        question = ""
                    self._append_resolved_market(
                        market_id=market_id,
                        question=question,
                        outcome=float(decision.outcome),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "resolved_markets_write_failed market={} error={}", market_id, exc,
                    )

            updates.append(
                ResolutionUpdate(
                    prediction_id=prediction_id,
                    market_id=market_id,
                    outcome=float(decision.outcome),
                    source=decision.source,
                    applied=applied,
                )
            )
            self._append_log(
                {
                    "timestamp": _utc_now_iso(),
                    "prediction_id": prediction_id,
                    "market_id": market_id,
                    "resolved": True,
                    "outcome": float(decision.outcome),
                    "source": decision.source,
                    "detail": decision.detail,
                    "applied": applied,
                }
            )

        resolved = len(updates)
        applied = sum(1 for item in updates if item.applied)
        unresolved = max(0, checked - resolved)

        return ResolutionRunReport(
            checked=checked,
            resolved=resolved,
            applied=applied,
            unresolved=unresolved,
            errors=errors,
            updates=updates,
        )

    def _append_resolved_market(self, market_id: str, question: str, outcome: float) -> None:
        """Append the resolution record to data/resolved_markets.jsonl.

        Carries entry_price/pnl_usdc from the most recent trades.jsonl entry
        for this market_id (if any), so downstream audit tools have the
        closing payoff in one place.
        """
        trade = _read_trade_entry(self.workspace_root, market_id)
        entry_price: float | None = None
        pnl_usdc: float | None = None
        if isinstance(trade, dict):
            raw_price = trade.get("fill_price") or trade.get("price")
            try:
                entry_price = float(raw_price) if raw_price is not None else None
            except (TypeError, ValueError):
                entry_price = None

            side = str(trade.get("side") or "").upper()
            raw_size = trade.get("fill_size") or trade.get("size_usdc") or 0.0
            try:
                size_usdc = float(raw_size)
            except (TypeError, ValueError):
                size_usdc = 0.0
            if entry_price is not None and size_usdc > 0:
                resolved_yes = bool(outcome >= 0.5)
                if side in {"BUY", "YES"}:
                    pnl_usdc = size_usdc * (1.0 - entry_price) if resolved_yes else -size_usdc * entry_price
                elif side in {"SELL", "NO"}:
                    pnl_usdc = -size_usdc * (1.0 - entry_price) if resolved_yes else size_usdc * entry_price

        payload: dict[str, Any] = {
            "market_id": market_id,
            "question": question,
            "resolved_yes": bool(outcome >= 0.5),
            "outcome": outcome,
            "resolution_timestamp": _utc_now_iso(),
            "entry_price": entry_price,
            "pnl_usdc": round(pnl_usdc, 6) if pnl_usdc is not None else None,
        }
        path = resolved_markets_path(self.workspace_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")

    def _append_log(self, payload: dict[str, Any]) -> None:
        path = self.workspace_root / "data" / "resolver_log.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")


def print_resolution_report(report: ResolutionRunReport) -> None:
    print(f"checked={report.checked}")
    print(f"resolved={report.resolved}")
    print(f"applied={report.applied}")
    print(f"unresolved={report.unresolved}")
    print(f"errors={report.errors}")
    if report.updates:
        print("updates:")
        for item in report.updates[:20]:
            print(json.dumps(asdict(item), sort_keys=True))


def write_resolution_report(workspace_root: Path, report: ResolutionRunReport) -> Path:
    path = resolver_report_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": _utc_now_iso(),
        "checked": report.checked,
        "resolved": report.resolved,
        "applied": report.applied,
        "unresolved": report.unresolved,
        "errors": report.errors,
        "updates": [asdict(item) for item in report.updates[:50]],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_resolution_report(workspace_root: Path) -> dict[str, Any] | None:
    path = resolver_report_path(workspace_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
