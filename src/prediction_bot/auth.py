from __future__ import annotations

import os
import importlib
from dataclasses import dataclass
from typing import Any


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


def load_auth_settings() -> AuthSettings:
    _load_dotenv_if_available()

    private_key = _required_env("POLYMARKET_PRIVATE_KEY")
    funder_address = _required_env("POLYMARKET_FUNDER_ADDRESS")
    anthropic_api_key = _required_env("ANTHROPIC_API_KEY")

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

    api_creds = client.create_or_derive_api_creds()
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


def verify_auth() -> bool:
    try:
        c = get_client()
        balance = c.get_balance()
        orders = c.get_open_orders()

        order_count = len(orders) if hasattr(orders, "__len__") else 0
        print(f"usdc_balance={_extract_balance_usdc(balance)}")
        print(f"open_orders_count={order_count}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"verify_auth_failed={exc}")
        return False
