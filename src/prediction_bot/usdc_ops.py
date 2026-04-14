from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


@dataclass(frozen=True)
class USDCCheckItem:
    name: str
    passed: bool
    severity: str
    detail: str


@dataclass(frozen=True)
class USDCCheckReport:
    timestamp: str
    ready: bool
    checks: list[USDCCheckItem]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_wallet_address(value: str | None) -> bool:
    if value is None:
        return False
    return bool(re.fullmatch(r"0x[a-fA-F0-9]{40}", value.strip()))


def _to_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _check_rpc(rpc_url: str | None, timeout: float = 8.0) -> tuple[bool, str]:
    if not rpc_url:
        return False, "missing_rpc_url"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}
    try:
        response = requests.post(rpc_url, json=payload, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"rpc_error:{exc}"

    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, str) and result.startswith("0x"):
        return True, f"chain_id={result}"
    return False, "rpc_no_chain_id"


def run_usdc_operational_checks(workspace_root: Path) -> USDCCheckReport:
    checks: list[USDCCheckItem] = []

    onramp_provider = os.getenv("USDC_ONRAMP_PROVIDER", "").strip()
    offramp_provider = os.getenv("USDC_OFFRAMP_PROVIDER", "").strip()
    wallet = os.getenv("POLYGON_WALLET_ADDRESS", "").strip()
    rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com").strip()

    daily_limit = _to_float(os.getenv("USDC_DAILY_TRANSFER_LIMIT"))
    max_single = _to_float(os.getenv("USDC_MAX_SINGLE_TRANSFER"))
    min_buffer = _to_float(os.getenv("USDC_MIN_BUFFER"))

    checks.append(
        USDCCheckItem(
            name="runbook_exists",
            passed=(workspace_root / "docs" / "usdc_onramp_runbook.md").exists(),
            severity="critical",
            detail="docs/usdc_onramp_runbook.md",
        )
    )
    checks.append(
        USDCCheckItem(
            name="onramp_provider_configured",
            passed=bool(onramp_provider),
            severity="critical",
            detail=onramp_provider or "missing USDC_ONRAMP_PROVIDER",
        )
    )
    checks.append(
        USDCCheckItem(
            name="offramp_provider_configured",
            passed=bool(offramp_provider),
            severity="critical",
            detail=offramp_provider or "missing USDC_OFFRAMP_PROVIDER",
        )
    )
    checks.append(
        USDCCheckItem(
            name="wallet_address_format",
            passed=_is_wallet_address(wallet),
            severity="critical",
            detail=wallet or "missing POLYGON_WALLET_ADDRESS",
        )
    )

    rpc_passed, rpc_detail = _check_rpc(rpc_url)
    checks.append(
        USDCCheckItem(
            name="polygon_rpc_connectivity",
            passed=rpc_passed,
            severity="critical",
            detail=rpc_detail,
        )
    )

    checks.append(
        USDCCheckItem(
            name="daily_transfer_limit",
            passed=(daily_limit is not None and daily_limit > 0),
            severity="critical",
            detail=str(daily_limit) if daily_limit is not None else "missing/invalid USDC_DAILY_TRANSFER_LIMIT",
        )
    )
    checks.append(
        USDCCheckItem(
            name="max_single_transfer",
            passed=(max_single is not None and max_single > 0),
            severity="critical",
            detail=str(max_single) if max_single is not None else "missing/invalid USDC_MAX_SINGLE_TRANSFER",
        )
    )
    checks.append(
        USDCCheckItem(
            name="min_buffer_configured",
            passed=(min_buffer is not None and min_buffer >= 0),
            severity="warning",
            detail=str(min_buffer) if min_buffer is not None else "missing USDC_MIN_BUFFER",
        )
    )

    critical_fails = [c for c in checks if c.severity == "critical" and not c.passed]
    report = USDCCheckReport(
        timestamp=_utc_now_iso(),
        ready=len(critical_fails) == 0,
        checks=checks,
    )

    out_path = workspace_root / "data" / "usdc_checks.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_payload: dict[str, Any] = asdict(report)
    out_path.write_text(json.dumps(out_payload, indent=2), encoding="utf-8")

    return report


def print_usdc_report(report: USDCCheckReport) -> None:
    print(f"timestamp={report.timestamp}")
    print(f"ready={report.ready}")
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"[{status}] {check.name} severity={check.severity} detail={check.detail}")
