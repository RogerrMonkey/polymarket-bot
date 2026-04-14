from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from prediction_bot.clients.http import HttpClient


class WatchlistManager:
    def __init__(self, http: HttpClient, watchlist_path: str | Path) -> None:
        self.http = http
        self.watchlist_path = Path(watchlist_path)

    def load_watchlist(self) -> list[str]:
        if not self.watchlist_path.exists():
            return []
        try:
            payload = json.loads(self.watchlist_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [str(x) for x in payload if x]

    def save_watchlist(self, condition_ids: list[str]) -> None:
        self.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        unique = []
        seen = set()
        for cid in condition_ids:
            val = str(cid).strip()
            if not val or val in seen:
                continue
            seen.add(val)
            unique.append(val)
        self.watchlist_path.write_text(json.dumps(unique, indent=2), encoding="utf-8")

    def refresh_watchlist(self) -> list[str]:
        payload = self.http.get_json(
            "https://gamma-api.polymarket.com/markets",
            params={
                "limit": "20",
                "active": "true",
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        ids = self._extract_condition_ids(payload)
        self.save_watchlist(ids)
        return ids

    def get_btc_5min_markets(self) -> list[str]:
        payload = self.http.get_json(
            "https://gamma-api.polymarket.com/markets",
            params={
                "search": "BTC 5-minute",
                "active": "true",
                "limit": "50",
            },
        )
        return self._extract_condition_ids(payload)

    def refresh_with_btc_priority(self) -> list[str]:
        top = self.refresh_watchlist()
        btc = self.get_btc_5min_markets()
        combined = btc + [x for x in top if x not in set(btc)]
        self.save_watchlist(combined)
        return combined

    @staticmethod
    def _extract_condition_ids(payload: Any) -> list[str]:
        if not isinstance(payload, list):
            return []

        out: list[str] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            cid = item.get("conditionId") or item.get("condition_id")
            if cid is None:
                continue
            out.append(str(cid))
        return out
