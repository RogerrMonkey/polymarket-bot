from __future__ import annotations

import sys
import types

import pytest

from prediction_bot import auth


class _FakeClobClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self._creds = None

    def create_or_derive_api_creds(self):
        return {"apiKey": "k", "secret": "s", "passphrase": "p"}

    def set_api_creds(self, creds):
        self._creds = creds

    def get_balance(self):
        return {"USDC": "42.0"}

    def get_open_orders(self):
        return []


def test_load_auth_settings_requires_values(monkeypatch):
    # Neuter dotenv so it cannot re-populate vars from the developer's real .env
    monkeypatch.setattr(auth, "_load_dotenv_if_available", lambda: None)
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_FUNDER_ADDRESS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        auth.load_auth_settings()


def test_create_client_derives_and_sets_api_creds(monkeypatch):
    monkeypatch.setenv(
        "POLYMARKET_PRIVATE_KEY",
        "0x" + "ab" * 32,  # 64 hex chars with 0x prefix
    )
    monkeypatch.setenv(
        "POLYMARKET_FUNDER_ADDRESS",
        "0x0000000000000000000000000000000000000001",
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.setenv("SIGNATURE_TYPE", "1")

    fake_pkg = types.ModuleType("py_clob_client")
    fake_client_mod = types.ModuleType("py_clob_client.client")
    fake_client_mod.ClobClient = _FakeClobClient

    monkeypatch.setitem(sys.modules, "py_clob_client", fake_pkg)
    monkeypatch.setitem(sys.modules, "py_clob_client.client", fake_client_mod)

    client = auth.create_client()
    assert isinstance(client, _FakeClobClient)
    assert client._creds == {"apiKey": "k", "secret": "s", "passphrase": "p"}


def test_verify_auth_success(monkeypatch):
    fake_client = _FakeClobClient()
    monkeypatch.setattr(auth, "client", fake_client)

    assert auth.verify_auth() is True


# --- v0.8.4 auth hardening ---


def test_normalize_private_key_strips_0x_prefix():
    normalized = auth._normalize_private_key("0x" + "ab" * 32)
    assert normalized == "ab" * 32
    assert not normalized.startswith("0x")


def test_normalize_private_key_rejects_bad_length():
    with pytest.raises(RuntimeError, match="64 hex chars"):
        auth._normalize_private_key("0xabc")


def test_normalize_funder_address_checksums_lowercase():
    # Valid lowercase address should be converted to EIP-55 checksum
    lower = "0x8ba1f109551bd432803012645ac136ddd64dba72"
    checksummed = auth._normalize_funder_address(lower)
    # Must be mixed-case checksum form
    assert checksummed.lower() == lower
    assert checksummed != lower  # was lowercase, now mixed


def test_normalize_funder_address_rejects_invalid():
    with pytest.raises(RuntimeError, match="invalid or not checksummable"):
        auth._normalize_funder_address("not-a-real-address")
