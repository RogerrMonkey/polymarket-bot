from __future__ import annotations

import json
from pathlib import Path

from prediction_bot.watchlist import WatchlistManager


class _FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def get_json(self, url: str, params=None):  # noqa: ANN001, ANN202
        self.calls.append((url, params))
        if params and params.get("search") == "BTC 5-minute":
            return [
                {"conditionId": "cid-btc-1"},
                {"conditionId": "cid-btc-2"},
            ]
        return [
            {"conditionId": "cid-top-1"},
            {"conditionId": "cid-top-2"},
            {"conditionId": "cid-top-3"},
        ]


def test_refresh_with_btc_priority_saves_file(tmp_path: Path) -> None:
    http = _FakeHttp()
    manager = WatchlistManager(http=http, watchlist_path=tmp_path / "watchlist.json")

    ids = manager.refresh_with_btc_priority()

    assert ids[:2] == ["cid-btc-1", "cid-btc-2"]
    assert (tmp_path / "watchlist.json").exists()

    stored = json.loads((tmp_path / "watchlist.json").read_text(encoding="utf-8"))
    assert stored == ids


def test_load_watchlist_handles_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text("{bad-json", encoding="utf-8")
    manager = WatchlistManager(http=_FakeHttp(), watchlist_path=path)

    assert manager.load_watchlist() == []
