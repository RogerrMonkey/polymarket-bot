from __future__ import annotations

import json
from pathlib import Path

import prediction_bot.alerting as alerting


def test_dispatch_skips_without_webhook(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BOT_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BOT_ALERT_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BOT_ALERT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("BOT_ALERT_TELEGRAM_CHAT_ID", raising=False)
    result = alerting.dispatch_alerts(tmp_path, ["warning:a"], source="test")
    assert result.skipped is True
    assert result.reason == "missing_channel_target"


def test_dispatch_dedup_signature(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_ALERT_WEBHOOK_URL", "https://example.invalid/hook")

    class _Resp:
        status_code = 200

    monkeypatch.setattr(alerting.requests, "post", lambda *a, **k: _Resp())

    first = alerting.dispatch_alerts(tmp_path, ["warning:a"], source="test")
    second = alerting.dispatch_alerts(tmp_path, ["warning:a"], source="test")

    assert first.sent is True
    assert second.skipped is True
    assert second.reason == "duplicate_signature"


def test_dispatch_writes_state_on_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_ALERT_WEBHOOK_URL", "https://example.invalid/hook")

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("network down")

    monkeypatch.setattr(alerting.requests, "post", _boom)

    result = alerting.dispatch_alerts(tmp_path, ["critical:x"], source="test", force=True)
    assert result.sent is False
    assert result.skipped is False
    assert "post_error" in result.reason

    state = json.loads((tmp_path / "data" / "alert_state.json").read_text(encoding="utf-8"))
    assert state["last_source"] == "test"


def test_dispatch_slack_channel(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_ALERT_CHANNEL", "slack")
    monkeypatch.setenv("BOT_ALERT_SLACK_WEBHOOK_URL", "https://example.invalid/slack")

    captured = {}

    class _Resp:
        status_code = 200

    def _post(url, json, timeout):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(alerting.requests, "post", _post)

    result = alerting.dispatch_alerts(tmp_path, ["warning:a"], source="test", force=True)
    assert result.sent is True
    assert result.channel == "slack"
    assert captured["url"] == "https://example.invalid/slack"
    assert "Prediction Bot Alerts" in captured["json"]["text"]


def test_dispatch_telegram_channel(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BOT_ALERT_CHANNEL", "telegram")
    monkeypatch.setenv("BOT_ALERT_TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("BOT_ALERT_TELEGRAM_CHAT_ID", "chat456")

    captured = {}

    class _Resp:
        status_code = 200

    def _post(url, json, timeout):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(alerting.requests, "post", _post)

    result = alerting.dispatch_alerts(tmp_path, ["critical:x"], source="test", force=True)
    assert result.sent is True
    assert result.channel == "telegram"
    assert captured["url"].endswith("/bottoken123/sendMessage")
    assert captured["json"]["chat_id"] == "chat456"
