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
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_FUNDER_ADDRESS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(RuntimeError):
        auth.load_auth_settings()


def test_create_client_derives_and_sets_api_creds(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xabc")
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", "0xdef")
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
