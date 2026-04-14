from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_verify_access() -> ModuleType:
    module_path = Path(__file__).resolve().parents[1] / "verify_access.py"
    spec = importlib.util.spec_from_file_location("verify_access", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load verify_access module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_access"] = module
    spec.loader.exec_module(module)
    return module


def test_first_market_title_prefers_question() -> None:
    verify_access = _load_verify_access()
    payload = [{"question": "Will BTC close above 70k?", "title": "fallback"}]
    assert verify_access._first_market_title(payload) == "Will BTC close above 70k?"


def test_check_http_market_pass(monkeypatch) -> None:
    verify_access = _load_verify_access()

    class _Response:
        status_code = 200

        @staticmethod
        def json() -> list[dict[str, str]]:
            return [{"title": "BTC 5-minute up/down"}]

    def _fake_get(url: str, timeout: int) -> _Response:  # noqa: ARG001
        return _Response()

    monkeypatch.setattr(verify_access.requests, "get", _fake_get)
    result = verify_access.check_http_market("test", "https://example.com")

    assert result.passed is True
    assert "status=200" in result.detail
    assert "BTC 5-minute up/down" in result.detail


def test_check_http_market_failure(monkeypatch) -> None:
    verify_access = _load_verify_access()

    def _fake_get(url: str, timeout: int) -> None:  # noqa: ARG001
        raise RuntimeError("network down")

    monkeypatch.setattr(verify_access.requests, "get", _fake_get)
    result = verify_access.check_http_market("test", "https://example.com")

    assert result.passed is False
    assert "error=network down" in result.detail


def test_summarize_exit_code() -> None:
    verify_access = _load_verify_access()
    ok = verify_access.CheckResult(name="one", passed=True, latency_ms=10.0, detail="ok")
    fail = verify_access.CheckResult(name="two", passed=False, latency_ms=10.0, detail="fail")

    assert verify_access.summarize_exit_code([ok]) == 0
    assert verify_access.summarize_exit_code([ok, fail]) == 1
