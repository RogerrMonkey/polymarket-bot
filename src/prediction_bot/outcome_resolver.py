from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prediction_bot.clients.http import HttpClient
from prediction_bot.storage.prediction_store import PredictionStore


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
        try:
            payload = self.http.get_json(f"{base}/markets/{market_id}")
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass

        try:
            payload = self.http.get_json(f"{base}/markets", params={"id": market_id, "limit": 1})
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                return payload[0]
        except Exception:
            pass

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

        rows = store.unresolved_predictions(limit=limit)
        checked = len(rows)

        for row in rows:
            prediction_id = int(row["id"])
            market_id = str(row["market_id"])

            try:
                decision = self.resolve_market_outcome(market_id)
            except Exception:  # noqa: BLE001
                errors += 1
                continue

            if not decision.resolved or decision.outcome not in {0.0, 1.0}:
                continue

            applied = False
            if not self.dry_run:
                applied = store.set_outcome(prediction_id=prediction_id, outcome=float(decision.outcome))
                # Close any matching paper-P&L entries for this market.
                # Lazy import keeps outcome_resolver importable even if paper_pnl is stripped.
                try:
                    from prediction_bot.paper_pnl import PaperPnLTracker  # local import

                    PaperPnLTracker().record_resolution(
                        market_id=market_id,
                        resolved_yes=bool(float(decision.outcome) >= 0.5),
                    )
                except Exception:  # noqa: BLE001
                    # P&L tracking must never block the resolver.
                    pass

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
