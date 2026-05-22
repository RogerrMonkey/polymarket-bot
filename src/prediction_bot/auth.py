from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass
from typing import Any

_HEX_PRIVATE_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class AuthSettings:
    private_key: str
    funder_address: str
    anthropic_api_key: str
    signature_type: int
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137


def _load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        # dotenv is optional; environment variables may already be present.
        return


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _normalize_private_key(raw: str) -> str:
    """Strip optional 0x prefix; reject obviously malformed hex."""
    candidate = raw.strip()
    if candidate.lower().startswith("0x"):
        candidate = candidate[2:]
    if not _HEX_PRIVATE_KEY_RE.match(candidate):
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY must be 64 hex chars (32 bytes), "
            "with or without 0x prefix"
        )
    return candidate


def _normalize_funder_address(raw: str) -> str:
    """Return EIP-55 checksummed address. Falls back to raw on import failure."""
    candidate = raw.strip()
    try:
        from web3 import Web3  # type: ignore

        return Web3.to_checksum_address(candidate)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"POLYMARKET_FUNDER_ADDRESS invalid or not checksummable: {exc}"
        ) from exc


def load_auth_settings() -> AuthSettings:
    _load_dotenv_if_available()

    private_key = _normalize_private_key(_required_env("POLYMARKET_PRIVATE_KEY"))
    funder_address = _normalize_funder_address(_required_env("POLYMARKET_FUNDER_ADDRESS"))
    # ANTHROPIC_API_KEY is optional here — Polymarket wallet auth does not need it.
    # Analyst provider (groq/anthropic/ollama) is resolved independently in llm_analyst.py.
    anthropic_api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

    signature_raw = os.getenv("SIGNATURE_TYPE", "1").strip()
    try:
        signature_type = int(signature_raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid SIGNATURE_TYPE value: {signature_raw}") from exc

    return AuthSettings(
        private_key=private_key,
        funder_address=funder_address,
        anthropic_api_key=anthropic_api_key,
        signature_type=signature_type,
    )


def _preloaded_api_creds() -> Any | None:
    """Return ApiCreds from env vars if all three are set, else None.

    The Polymarket CLOB API-key derivation endpoint is geo-blocked by
    Cloudflare from some regions (notably India) even with WARP active.
    The market-data endpoints stay reachable, so we can avoid the
    derive call entirely if the operator already has credentials from
    a prior browser session and pastes them into .env:

        POLYMARKET_API_KEY=...
        POLYMARKET_API_SECRET=...
        POLYMARKET_API_PASSPHRASE=...
    """
    api_key = (os.getenv("POLYMARKET_API_KEY") or "").strip()
    api_secret = (os.getenv("POLYMARKET_API_SECRET") or "").strip()
    api_passphrase = (os.getenv("POLYMARKET_API_PASSPHRASE") or "").strip()
    if not (api_key and api_secret and api_passphrase):
        return None
    try:
        clob_types = importlib.import_module("py_clob_client.clob_types")
        ApiCreds = getattr(clob_types, "ApiCreds")
    except Exception:  # noqa: BLE001
        return None
    return ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)


def create_client() -> Any:
    settings = load_auth_settings()

    try:
        client_module = importlib.import_module("py_clob_client.client")
        ClobClient = getattr(client_module, "ClobClient")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "py-clob-client is required for authenticated Polymarket access. Install with: pip install py-clob-client"
        ) from exc

    try:
        client = ClobClient(
            host=settings.host,
            chain_id=settings.chain_id,
            key=settings.private_key,
            signature_type=settings.signature_type,
            funder=settings.funder_address,
        )
    except TypeError:
        # Compatibility for versions that accept positional host argument.
        client = ClobClient(
            settings.host,
            chain_id=settings.chain_id,
            key=settings.private_key,
            signature_type=settings.signature_type,
            funder=settings.funder_address,
        )

    # Prefer pre-loaded creds when available — bypasses the Cloudflare-blocked
    # /auth/api-key/create endpoint that returns 403 from geo-restricted IPs.
    preloaded = _preloaded_api_creds()
    if preloaded is not None:
        client.set_api_creds(preloaded)
        return client

    try:
        api_creds = client.create_or_derive_api_creds()
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "403" in msg or "Could not derive" in msg:
            raise RuntimeError(
                "CLOB auth endpoint blocked (Cloudflare 403 / 'Could not derive api key!'). "
                "This is typically a geo-restriction even with WARP. Workaround: create API "
                "credentials once on polymarket.com (browser console or web app), then paste "
                "them into .env as POLYMARKET_API_KEY / POLYMARKET_API_SECRET / "
                "POLYMARKET_API_PASSPHRASE. The bot will use them directly without re-deriving."
            ) from exc
        raise
    client.set_api_creds(api_creds)
    return client


client: Any | None = None


def get_client() -> Any:
    global client
    if client is None:
        client = create_client()
    return client


def _extract_balance_usdc(balance_payload: Any) -> str:
    if isinstance(balance_payload, dict):
        for key in ("balance", "available", "USDC", "usdc", "amount"):
            if key in balance_payload:
                return str(balance_payload[key])
    return str(balance_payload)


def _call_balance(client: Any) -> Any:
    """Version-tolerant USDC balance read for py-clob-client.

    Modern versions (>=0.17) expose get_balance_allowance(BalanceAllowanceParams(asset_type=COLLATERAL));
    older forks had a plain get_balance(). Try both.
    """
    if hasattr(client, "get_balance"):
        return client.get_balance()
    if hasattr(client, "get_balance_allowance"):
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams  # type: ignore

            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            return client.get_balance_allowance(params)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"get_balance_allowance failed: {exc}") from exc
    raise RuntimeError("ClobClient has neither get_balance nor get_balance_allowance")


def _call_open_orders(client: Any) -> Any:
    """Version-tolerant open-orders read."""
    if hasattr(client, "get_open_orders"):
        return client.get_open_orders()
    if hasattr(client, "get_orders"):
        return client.get_orders()
    raise RuntimeError("ClobClient has neither get_open_orders nor get_orders")


def verify_auth() -> bool:
    try:
        c = get_client()
        balance = _call_balance(c)
        orders = _call_open_orders(c)

        order_count = len(orders) if hasattr(orders, "__len__") else 0
        print(f"usdc_balance={_extract_balance_usdc(balance)}")
        print(f"open_orders_count={order_count}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"verify_auth_failed={exc}")
        return False
