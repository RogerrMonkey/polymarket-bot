from __future__ import annotations

from pathlib import Path

import prediction_bot.usdc_ops as usdc_ops


def test_wallet_address_validation() -> None:
    assert usdc_ops._is_wallet_address("0x" + "a" * 40) is True
    assert usdc_ops._is_wallet_address("0x123") is False


def test_run_usdc_checks_reports_missing_required(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("USDC_ONRAMP_PROVIDER", raising=False)
    monkeypatch.delenv("USDC_OFFRAMP_PROVIDER", raising=False)
    monkeypatch.delenv("POLYGON_WALLET_ADDRESS", raising=False)
    monkeypatch.delenv("USDC_DAILY_TRANSFER_LIMIT", raising=False)
    monkeypatch.delenv("USDC_MAX_SINGLE_TRANSFER", raising=False)

    monkeypatch.setattr(usdc_ops, "_check_rpc", lambda rpc_url, timeout=8.0: (True, "chain_id=0x89"))

    report = usdc_ops.run_usdc_operational_checks(tmp_path)
    assert report.ready is False
    assert any(c.name == "onramp_provider_configured" and not c.passed for c in report.checks)


def test_run_usdc_checks_ready(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "usdc_onramp_runbook.md").write_text("ok", encoding="utf-8")

    monkeypatch.setenv("USDC_ONRAMP_PROVIDER", "manual-p2p")
    monkeypatch.setenv("USDC_OFFRAMP_PROVIDER", "manual-p2p")
    monkeypatch.setenv("POLYGON_WALLET_ADDRESS", "0x" + "b" * 40)
    monkeypatch.setenv("USDC_DAILY_TRANSFER_LIMIT", "500")
    monkeypatch.setenv("USDC_MAX_SINGLE_TRANSFER", "100")
    monkeypatch.setenv("USDC_MIN_BUFFER", "20")

    monkeypatch.setattr(usdc_ops, "_check_rpc", lambda rpc_url, timeout=8.0: (True, "chain_id=0x89"))

    report = usdc_ops.run_usdc_operational_checks(tmp_path)
    assert report.ready is True
    assert (tmp_path / "data" / "usdc_checks.json").exists()
