from __future__ import annotations

import os
import re
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from prediction_bot.paper_metrics import check_paper_gates
from prediction_bot.risk_engine import RiskConfig


@dataclass(frozen=True)
class ChecklistItem:
    name: str
    passed: bool
    detail: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _warp_hint_if_dns(detail: str) -> str:
    """Append a WARP hint when the detail looks like a DNS/connection failure.

    Polymarket + polygon.llamarpc are routinely DNS-blocked from India; Cloudflare
    WARP lifts the block locally. Surface that hint on the check so the operator
    knows the fix instead of chasing a generic network error.
    """
    lower = detail.lower()
    triggers = ("getaddrinfo failed", "failed to resolve", "nameresolutionerror", "name or service not known")
    if any(t in lower for t in triggers):
        return "network_unreachable — enable Cloudflare WARP and retry; " + detail
    return detail


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _check_dry_run_false() -> ChecklistItem:
    raw = os.getenv("DRY_RUN")
    parsed = _parse_bool(raw)
    if raw is None:
        return ChecklistItem("env_dry_run_false", False, "DRY_RUN missing (must be explicitly false)")
    if parsed is False:
        return ChecklistItem("env_dry_run_false", True, f"DRY_RUN={raw}")
    if parsed is True:
        return ChecklistItem("env_dry_run_false", False, f"DRY_RUN={raw} (must be false)")
    return ChecklistItem("env_dry_run_false", False, f"DRY_RUN={raw} (invalid bool)")


def _check_polygon_rpc() -> ChecklistItem:
    rpc_url = os.getenv("POLYGON_RPC_URL", "").strip()
    if not rpc_url:
        return ChecklistItem("env_polygon_rpc_responsive", False, "POLYGON_RPC_URL missing")

    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    try:
        response = requests.post(rpc_url, json=payload, timeout=10)
        status = response.status_code
        data = response.json() if response.content else {}
    except Exception as exc:  # noqa: BLE001
        return ChecklistItem("env_polygon_rpc_responsive", False, _warp_hint_if_dns(f"rpc_error:{exc}"))

    chain_id = data.get("result") if isinstance(data, dict) else None
    passed = 200 <= status < 300 and isinstance(chain_id, str) and chain_id.startswith("0x")
    detail = f"status={status} chain_id={chain_id}"
    return ChecklistItem("env_polygon_rpc_responsive", passed, detail)


def _check_polymarket_envs() -> list[ChecklistItem]:
    required = [
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_FUNDER_ADDRESS",
        "SIGNATURE_TYPE",
    ]
    checks: list[ChecklistItem] = []

    for name in required:
        value = os.getenv(name)
        checks.append(
            ChecklistItem(
                name=f"env_{name.lower()}_present",
                passed=bool(value and value.strip()),
                detail="present" if value and value.strip() else "missing",
            )
        )

    return checks


def _check_analyst_provider_resolved() -> ChecklistItem:
    """Pass if the head of the provider chain is something other than the deterministic stub."""
    try:
        from prediction_bot.claude_analyst import StubProvider, build_provider_chain
    except Exception as exc:  # noqa: BLE001
        return ChecklistItem("analyst_provider_resolved", False, f"import_failed:{exc}")

    try:
        chain = build_provider_chain()
    except Exception as exc:  # noqa: BLE001
        return ChecklistItem("analyst_provider_resolved", False, f"chain_build_failed:{exc}")

    if not chain or isinstance(chain[0], StubProvider):
        return ChecklistItem(
            "analyst_provider_resolved",
            False,
            "No analyst provider configured — set GROQ_API_KEY, ANTHROPIC_API_KEY, or ensure Ollama is reachable",
        )

    head = chain[0]
    return ChecklistItem(
        "analyst_provider_resolved",
        True,
        f"provider={head.name} model={getattr(head, 'model', 'n/a')}",
    )


def _extract_numeric(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if not match:
            return None
        try:
            return float(match.group(0))
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("available", "balance", "USDC", "usdc", "amount"):
            if key in value:
                parsed = _extract_numeric(value.get(key))
                if parsed is not None:
                    return parsed
        return None
    if isinstance(value, list):
        for item in value:
            parsed = _extract_numeric(item)
            if parsed is not None:
                return parsed
    return None


def _check_balance_and_open_orders() -> list[ChecklistItem]:
    try:
        from prediction_bot.auth import get_client
    except Exception as exc:  # noqa: BLE001
        return [
            ChecklistItem("balance_usdc_gt_20", False, f"auth_import_failed:{exc}"),
            ChecklistItem("open_orders_empty", False, "auth_import_failed"),
        ]

    try:
        client = get_client()
        balance_payload = client.get_balance()
        open_orders = client.get_open_orders()
    except Exception as exc:  # noqa: BLE001
        return [
            ChecklistItem("balance_usdc_gt_20", False, f"auth_call_failed:{exc}"),
            ChecklistItem("open_orders_empty", False, "auth_call_failed"),
        ]

    balance_value = _extract_numeric(balance_payload)
    if isinstance(open_orders, dict):
        orders_count = len(open_orders.get("data", [])) if isinstance(open_orders.get("data"), list) else len(open_orders)
    else:
        orders_count = len(open_orders) if hasattr(open_orders, "__len__") else 0

    checks = [
        ChecklistItem(
            "balance_usdc_gt_20",
            passed=(balance_value is not None and balance_value > 20.0),
            detail=f"balance={balance_value}",
        ),
        ChecklistItem(
            "open_orders_empty",
            passed=(orders_count == 0),
            detail=f"open_orders={orders_count}",
        ),
    ]
    return checks


def _check_risk_config(workspace_root: Path) -> list[ChecklistItem]:
    cfg = RiskConfig.from_json_file(workspace_root / "risk_config.json")
    return [
        ChecklistItem("risk_kill_switch_false", not cfg.kill_switch, f"kill_switch={cfg.kill_switch}"),
        ChecklistItem(
            "risk_daily_loss_cap_range",
            0.01 <= cfg.daily_loss_cap_pct <= 0.10,
            f"daily_loss_cap_pct={cfg.daily_loss_cap_pct}",
        ),
        ChecklistItem(
            "risk_max_position_pct",
            cfg.max_position_pct <= 0.15,
            f"max_position_pct={cfg.max_position_pct}",
        ),
        ChecklistItem(
            "risk_kelly_fraction",
            cfg.kelly_fraction <= 0.50,
            f"kelly_fraction={cfg.kelly_fraction}",
        ),
    ]


def _analysis_days(workspace_root: Path) -> int:
    path = workspace_root / "data" / "analyses.jsonl"
    if not path.exists():
        return 0

    days: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(row, dict):
            continue
        ts = str(row.get("timestamp") or "").strip()
        if not ts:
            continue
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days.add(dt.astimezone(timezone.utc).date().isoformat())

    return len(days)


def _check_paper_loop_has_run_today(workspace_root: Path) -> ChecklistItem:
    """Pass if analyses.jsonl has at least one entry timestamped today (UTC).

    Acts as a live health signal that the daily scheduler is actually firing.
    """
    path = workspace_root / "data" / "analyses.jsonl"
    if not path.exists():
        return ChecklistItem("paper_loop_has_run_today", False, "analyses.jsonl missing")

    today = datetime.now(timezone.utc).date().isoformat()
    last_ts_today: str | None = None

    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(row, dict):
            continue
        ts = str(row.get("timestamp") or "").strip()
        if not ts:
            continue
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.astimezone(timezone.utc).date().isoformat() == today:
            last_ts_today = ts

    if last_ts_today is not None:
        return ChecklistItem("paper_loop_has_run_today", True, f"last_run_today={last_ts_today}")
    return ChecklistItem(
        "paper_loop_has_run_today",
        False,
        f"no_analyses_for_{today} — scheduler may be down",
    )


def _check_news_feed_has_sources(workspace_root: Path) -> ChecklistItem:
    """Pass if the news pipeline returns at least one item from GDELT or RSS."""
    from prediction_bot.clients.http import HttpClient
    from prediction_bot.config import load_config
    from prediction_bot.research.news_feed import GDELTFetcher, RSSFetcher

    config = load_config()
    http = HttpClient(
        timeout_seconds=config.runtime.request_timeout_seconds,
        user_agent=config.runtime.user_agent,
    )

    gdelt_count = 0
    rss_count = 0
    try:
        gdelt_count = len(GDELTFetcher(http=http, query=config.research.gdelt_query).fetch_once(limit=5))
    except Exception:  # noqa: BLE001
        gdelt_count = 0
    try:
        rss_count = len(RSSFetcher(http=http).fetch_once(limit=5))
    except Exception:  # noqa: BLE001
        rss_count = 0

    total = gdelt_count + rss_count
    if total > 0:
        return ChecklistItem(
            "news_feed_has_sources",
            True,
            f"gdelt={gdelt_count} rss={rss_count}",
        )
    return ChecklistItem(
        "news_feed_has_sources",
        False,
        "no news items from GDELT or RSS — check network/WARP and BOT_RSS_FEEDS",
    )


def _check_scheduled_job_registered() -> ChecklistItem:
    """Windows-only: pass if PolymarketPaperLoop Scheduled Task is registered.

    Gracefully skips with PASS on non-Windows (no scheduler expected there).
    Uses `schtasks /query` so we don't depend on PowerShell COM access.
    """
    import platform
    import subprocess

    name = "scheduled_job_registered"
    if platform.system() != "Windows":
        return ChecklistItem(name, True, "non_windows_skip")

    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", "PolymarketPaperLoop", "/fo", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return ChecklistItem(name, False, "schtasks_cli_not_found")
    except subprocess.TimeoutExpired:
        return ChecklistItem(name, False, "schtasks_query_timeout")
    except Exception as exc:  # noqa: BLE001
        return ChecklistItem(name, False, f"schtasks_query_error:{exc}")

    if result.returncode != 0:
        hint = (result.stderr or result.stdout or "").strip().splitlines()[:1]
        detail = f"not_registered exit={result.returncode} {hint[0] if hint else ''}".strip()
        return ChecklistItem(name, False, detail)

    status_line = ""
    for line in (result.stdout or "").splitlines():
        if line.lower().startswith("status"):
            status_line = line.split(":", 1)[-1].strip()
            break
    return ChecklistItem(name, True, f"registered status={status_line or 'unknown'}")


def _check_paper_gates(workspace_root: Path, db_path: str) -> list[ChecklistItem]:
    ready, failures = check_paper_gates(workspace_root=workspace_root, db_path=db_path)
    days = _analysis_days(workspace_root)

    return [
        ChecklistItem(
            "paper_gates_passed",
            ready,
            "none" if not failures else "|".join(failures),
        ),
        ChecklistItem(
            "paper_minimum_14_days",
            days >= 14,
            f"analysis_days={days}",
        ),
    ]


def _check_access() -> list[ChecklistItem]:
    try:
        from verify_access import CLOB_MARKETS_URL, GAMMA_MARKETS_URL, check_http_market, check_websocket
    except Exception as exc:  # noqa: BLE001
        return [ChecklistItem("access_checks", False, f"verify_access_import_failed:{exc}")]

    clob = check_http_market("clob_markets", CLOB_MARKETS_URL)
    gamma = check_http_market("gamma_markets", GAMMA_MARKETS_URL)
    ws = check_websocket()

    return [
        ChecklistItem("access_clob_markets", clob.passed, _warp_hint_if_dns(clob.detail)),
        ChecklistItem("access_gamma_markets", gamma.passed, _warp_hint_if_dns(gamma.detail)),
        ChecklistItem("access_websocket", ws.passed, _warp_hint_if_dns(ws.detail)),
    ]


def collect_pre_live_checks(workspace_root: Path, db_path: str) -> list[ChecklistItem]:
    checks: list[ChecklistItem] = []
    checks.append(_check_dry_run_false())
    checks.append(_check_polygon_rpc())
    checks.extend(_check_polymarket_envs())
    checks.append(_check_analyst_provider_resolved())
    checks.extend(_check_balance_and_open_orders())
    checks.extend(_check_risk_config(workspace_root))
    checks.extend(_check_paper_gates(workspace_root, db_path))
    checks.append(_check_paper_loop_has_run_today(workspace_root))
    checks.append(_check_news_feed_has_sources(workspace_root))
    checks.append(_check_scheduled_job_registered())
    checks.extend(_check_access())
    return checks


def run_pre_live_checklist(workspace_root: Path, db_path: str) -> tuple[bool, list[ChecklistItem]]:
    checks = collect_pre_live_checks(workspace_root=workspace_root, db_path=db_path)
    all_passed = all(item.passed for item in checks)
    return all_passed, checks


def pre_live_report_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "prelive_checklist.json"


def build_pre_live_report_payload(checks: list[ChecklistItem], all_passed: bool) -> dict[str, Any]:
    passed_count = sum(1 for c in checks if c.passed)
    failed_count = len(checks) - passed_count
    return {
        "timestamp": _utc_now_iso(),
        "all_passed": all_passed,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "detail": c.detail,
            }
            for c in checks
        ],
    }


def write_pre_live_report(workspace_root: Path, checks: list[ChecklistItem], all_passed: bool) -> Path:
    path = pre_live_report_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_pre_live_report_payload(checks=checks, all_passed=all_passed)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_pre_live_report(workspace_root: Path) -> dict[str, Any] | None:
    path = pre_live_report_path(workspace_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def print_pre_live_report(checks: list[ChecklistItem], all_passed: bool) -> None:
    print(f"timestamp={_utc_now_iso()}")
    print("prelive_checks:")
    for item in checks:
        status = "PASS" if item.passed else "FAIL"
        print(f"  [{status}] {item.name} detail={item.detail}")

    if all_passed:
        print("ALL CHECKS PASSED - READY FOR LIVE")
    else:
        print("LIVE TRADING BLOCKED")
