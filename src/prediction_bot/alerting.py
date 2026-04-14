from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


@dataclass(frozen=True)
class AlertDispatchResult:
    sent: bool
    skipped: bool
    reason: str
    alerts_count: int
    webhook_status: int | None
    channel: str = "webhook"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "alert_state.json"


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _signature(alerts: list[str]) -> str:
    normalized = json.dumps(sorted(alerts), separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _active_channel() -> str:
    return os.getenv("BOT_ALERT_CHANNEL", "webhook").strip().lower()


def _channel_target(channel: str) -> str | None:
    if channel == "slack":
        return os.getenv("BOT_ALERT_SLACK_WEBHOOK_URL", "").strip() or os.getenv("BOT_ALERT_WEBHOOK_URL", "").strip()
    if channel == "webhook":
        return os.getenv("BOT_ALERT_WEBHOOK_URL", "").strip()
    if channel == "telegram":
        token = os.getenv("BOT_ALERT_TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("BOT_ALERT_TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return None
        return f"https://api.telegram.org/bot{token}/sendMessage"
    return None


def _format_text(source: str, alerts: list[str]) -> str:
    lines = [f"Prediction Bot Alerts ({source})", ""]
    for item in alerts:
        lines.append(f"- {item}")
    return "\n".join(lines)


def _channel_payload(channel: str, source: str, alerts: list[str]) -> dict[str, Any]:
    payload = {
        "timestamp": _utc_now_iso(),
        "source": source,
        "alerts": alerts,
        "count": len(alerts),
    }
    if channel == "slack":
        return {
            "text": _format_text(source, alerts),
            "metadata": payload,
        }
    if channel == "telegram":
        return {
            "chat_id": os.getenv("BOT_ALERT_TELEGRAM_CHAT_ID", "").strip(),
            "text": _format_text(source, alerts),
            "disable_web_page_preview": True,
        }
    return payload


def dispatch_alerts(
    workspace_root: Path,
    alerts: list[str],
    source: str,
    force: bool = False,
) -> AlertDispatchResult:
    channel = _active_channel()
    target = _channel_target(channel)
    if not target:
        return AlertDispatchResult(
            sent=False,
            skipped=True,
            reason="missing_channel_target",
            alerts_count=len(alerts),
            webhook_status=None,
            channel=channel,
        )

    if not alerts:
        return AlertDispatchResult(
            sent=False,
            skipped=True,
            reason="no_alerts",
            alerts_count=0,
            webhook_status=None,
            channel=channel,
        )

    allow_repeat = os.getenv("BOT_ALERTS_ALLOW_REPEAT", "false").strip().lower() in {"1", "true", "yes", "on"}
    state_path = _state_path(workspace_root)
    state = _read_state(state_path)

    sig = _signature(alerts)
    if not force and not allow_repeat and state.get("last_signature") == sig:
        return AlertDispatchResult(
            sent=False,
            skipped=True,
            reason="duplicate_signature",
            alerts_count=len(alerts),
            webhook_status=None,
            channel=channel,
        )

    payload = _channel_payload(channel=channel, source=source, alerts=alerts)

    try:
        response = requests.post(target, json=payload, timeout=10)
        status = response.status_code
        ok = 200 <= status < 300
    except Exception as exc:  # noqa: BLE001
        _write_state(
            state_path,
            {
                "last_signature": sig,
                "last_sent_at": _utc_now_iso(),
                "last_source": source,
                "last_result": f"error:{exc}",
                "last_count": len(alerts),
                "last_status": None,
                "last_channel": channel,
            },
        )
        return AlertDispatchResult(
            sent=False,
            skipped=False,
            reason=f"post_error:{exc}",
            alerts_count=len(alerts),
            webhook_status=None,
            channel=channel,
        )

    _write_state(
        state_path,
        {
            "last_signature": sig,
            "last_sent_at": _utc_now_iso(),
            "last_source": source,
            "last_result": "sent" if ok else "http_error",
            "last_count": len(alerts),
            "last_status": status,
            "last_channel": channel,
        },
    )

    if ok:
        return AlertDispatchResult(
            sent=True,
            skipped=False,
            reason="sent",
            alerts_count=len(alerts),
            webhook_status=status,
            channel=channel,
        )

    return AlertDispatchResult(
        sent=False,
        skipped=False,
        reason="http_error",
        alerts_count=len(alerts),
        webhook_status=status,
        channel=channel,
    )


def read_alert_state(workspace_root: Path) -> dict[str, Any]:
    return _read_state(_state_path(workspace_root))


def print_alert_dispatch(result: AlertDispatchResult) -> None:
    print(f"channel={result.channel}")
    print(f"sent={result.sent}")
    print(f"skipped={result.skipped}")
    print(f"reason={result.reason}")
    print(f"alerts_count={result.alerts_count}")
    print(f"webhook_status={result.webhook_status}")
