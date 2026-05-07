"""Smart money signal — top-trader awareness layer.

Tracks high-volume Polymarket traders (proxy wallets that show up
frequently in the public trades feed) and looks up their current
positions in markets we are about to analyse. Aggregated as a
SmartMoneySignal that the LLM analyst sees in its prompt and that
the risk engine uses as a post-analysis modifier.

Why volume-rank instead of PnL-rank: Polymarket's PnL-ranked
/profiles endpoint is auth-gated (HTTP 401). The public /trades
feed is the only public source of trader identity at scale, so
we approximate "top traders" as "wallets with the highest recent
trading volume" — same direction of signal, fully public data.

Two HTTP endpoints, both public:
    https://data-api.polymarket.com/trades?limit=N
    https://data-api.polymarket.com/positions?user=<proxy>&sizeThreshold=0
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from prediction_bot.clients.http import HttpClient


# --- Tunables (env-overridable) ----------------------------------------------


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


SM_TOP_TRADERS_LIMIT = _env_int("SM_TOP_TRADERS_LIMIT", 20)
SM_MIN_POSITION_USDC = _env_float("SM_MIN_POSITION_USDC", 200.0)
SM_CACHE_TTL_HOURS = _env_float("SM_CACHE_TTL_HOURS", 6.0)
SM_TRADES_SAMPLE_SIZE = _env_int("SM_TRADES_SAMPLE_SIZE", 500)


def _cache_path(workspace_root: Path | None = None) -> Path:
    root = workspace_root if workspace_root is not None else Path(".")
    return root / "data" / "top_traders.json"


def _signal_log_path(workspace_root: Path | None = None) -> Path:
    root = workspace_root if workspace_root is not None else Path(".")
    return root / "data" / "smart_money.jsonl"


# --- Data classes ------------------------------------------------------------


@dataclass(frozen=True)
class TopTrader:
    address: str
    rank: int
    pnl_usdc: float          # all-time profit (0.0 when ranking by volume)
    total_trades: int
    win_rate: float          # 0.0 when not derivable from public feed
    roi_pct: float           # placeholder, 0.0 when ranking by volume
    weight: float            # log(1 + volume_or_roi); min 0.1


@dataclass(frozen=True)
class TraderPosition:
    address: str
    market_id: str           # condition_id (positions feed keys on this)
    side: str                # "YES" or "NO"
    size_usdc: float
    entry_price: float
    opened_at: str           # ISO timestamp; "" if unknown
    is_recent: bool          # opened within last 24h


@dataclass
class SmartMoneySignal:
    market_id: str           # condition_id when available, else internal id
    traders_present: int
    weighted_yes_prob: float
    consensus_strength: str  # "Low" / "Medium" / "High"
    largest_position_usdc: float
    recent_entries_24h: int
    total_smart_money_usdc: float
    top_trader_sides: list[str] = field(default_factory=list)
    fetched_at: str = ""
    cache_hit: bool = False


# --- Helpers ------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _is_recent(opened_at: str, now: datetime | None = None) -> bool:
    dt = _parse_iso(opened_at)
    if dt is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return (ref - dt).total_seconds() <= 24 * 3600


def _normalise_side_from_outcome(outcome: Any, outcome_index: Any) -> str:
    """Polymarket positions return outcome as 'Yes'/'No' or use outcomeIndex.

    YES = outcomeIndex 0; NO = 1 in the standard binary market layout.
    """
    o = (str(outcome) or "").strip().lower()
    if o == "yes":
        return "YES"
    if o == "no":
        return "NO"
    try:
        idx = int(outcome_index)
        return "YES" if idx == 0 else "NO"
    except (TypeError, ValueError):
        return "YES"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --- fetch_top_traders -------------------------------------------------------


def fetch_top_traders(
    limit: int = SM_TOP_TRADERS_LIMIT,
    *,
    http: HttpClient | None = None,
    workspace_root: Path | None = None,
    sample_size: int = SM_TRADES_SAMPLE_SIZE,
    cache_ttl_hours: float = SM_CACHE_TTL_HOURS,
) -> list[TopTrader]:
    """Return the top `limit` proxy wallets by recent trading volume.

    Approximates "top traders" via volume on /trades since /profiles is
    auth-gated. Cached to data/top_traders.json with `cache_ttl_hours`.
    Never raises — returns [] on any failure.
    """
    cache_path = _cache_path(workspace_root)
    now = time.time()

    # 1. Cache hit
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_at = float(payload.get("cached_at_unix") or 0)
            if (now - cached_at) < cache_ttl_hours * 3600:
                rows = payload.get("traders") or []
                cached = [TopTrader(**r) for r in rows][:limit]
                logger.info(
                    "smart_money: fetched {} top traders (top_volume=${:.0f}, cached=True)",
                    len(cached),
                    cached[0].pnl_usdc if cached else 0.0,
                )
                return cached
        except Exception as exc:  # noqa: BLE001
            logger.debug("smart_money_cache_unreadable error={}", exc)

    # 2. Live fetch via /trades
    client = http if http is not None else HttpClient(timeout_seconds=10.0, user_agent="polymarket-bot/0.9.3")
    try:
        payload = client.get_json(
            "https://data-api.polymarket.com/trades",
            params={"limit": str(int(sample_size))},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("smart_money_trades_fetch_failed error={}", exc)
        return []

    if not isinstance(payload, list):
        logger.warning("smart_money_trades_unexpected_shape type={}", type(payload).__name__)
        return []

    # 3. Aggregate by proxyWallet
    by_wallet: dict[str, dict[str, float]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        addr = str(row.get("proxyWallet") or "").lower()
        if not addr:
            continue
        size = _safe_float(row.get("size"))
        price = _safe_float(row.get("price"))
        usd = size * price if price > 0 else size  # fall back to share count if price missing
        agg = by_wallet.setdefault(addr, {"trades": 0.0, "volume_usdc": 0.0})
        agg["trades"] += 1
        agg["volume_usdc"] += usd

    # 4. Rank by volume_usdc desc
    ranked = sorted(by_wallet.items(), key=lambda kv: kv[1]["volume_usdc"], reverse=True)[:limit]

    traders: list[TopTrader] = []
    for rank, (addr, agg) in enumerate(ranked, start=1):
        volume_usdc = float(agg["volume_usdc"])
        weight = max(math.log(1.0 + max(volume_usdc, 0.0)) / 10.0, 0.1)
        traders.append(
            TopTrader(
                address=addr,
                rank=rank,
                pnl_usdc=round(volume_usdc, 2),  # volume proxy in this slot; see docstring
                total_trades=int(agg["trades"]),
                win_rate=0.0,
                roi_pct=0.0,
                weight=round(weight, 4),
            )
        )

    # 5. Persist cache (best-effort)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "cached_at_unix": now,
                    "cached_at_iso": _utc_now_iso(),
                    "ranking_method": "recent_volume_usdc",
                    "sample_size": sample_size,
                    "traders": [asdict(t) for t in traders],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("smart_money_cache_write_failed error={}", exc)

    logger.info(
        "smart_money: fetched {} top traders (top_volume=${:.0f}, cached=False)",
        len(traders),
        traders[0].pnl_usdc if traders else 0.0,
    )
    return traders


# --- fetch_trader_position ---------------------------------------------------


def fetch_trader_position(
    address: str,
    market_id: str,
    *,
    http: HttpClient | None = None,
    seen: set[tuple[str, str]] | None = None,
    min_position_usdc: float = SM_MIN_POSITION_USDC,
) -> TraderPosition | None:
    """Return a TraderPosition for `address` in market `market_id` (condition_id).

    Returns None when the trader has no qualifying position. Never raises.
    Uses an external `seen` set to dedupe duplicate calls within a cycle.
    """
    addr_lc = (address or "").strip().lower()
    mkt_lc = (market_id or "").strip().lower()
    if not addr_lc or not mkt_lc:
        return None

    if seen is not None:
        key = (addr_lc, mkt_lc)
        if key in seen:
            return None
        seen.add(key)

    client = http if http is not None else HttpClient(timeout_seconds=10.0, user_agent="polymarket-bot/0.9.3")
    try:
        payload = client.get_json(
            "https://data-api.polymarket.com/positions",
            params={"user": address, "sizeThreshold": "0"},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("smart_money_position_fetch_failed addr={} mkt={} error={}", addr_lc[:14], mkt_lc[:14], exc)
        return None

    if not isinstance(payload, list):
        return None

    for row in payload:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("conditionId") or "").lower()
        if cid != mkt_lc:
            continue
        size_shares = _safe_float(row.get("size"))
        avg_price = _safe_float(row.get("avgPrice"))
        # current value gives us the live USDC notional; fall back to size*avgPrice.
        size_usdc = _safe_float(row.get("currentValue")) or (size_shares * avg_price)
        if size_usdc < min_position_usdc:
            continue
        opened_at = str(row.get("startTimestamp") or row.get("startTime") or "")
        return TraderPosition(
            address=addr_lc,
            market_id=mkt_lc,
            side=_normalise_side_from_outcome(row.get("outcome"), row.get("outcomeIndex")),
            size_usdc=round(size_usdc, 2),
            entry_price=round(avg_price, 4),
            opened_at=opened_at,
            is_recent=_is_recent(opened_at),
        )
    return None


# --- fetch_smart_money_signal ------------------------------------------------


def _consensus_strength(traders_present: int, total_usdc: float) -> str:
    if traders_present == 0:
        return "Low"
    if traders_present >= 3 and total_usdc >= 1000:
        return "High"
    if traders_present >= 2 or total_usdc >= 500:
        return "Medium"
    return "Low"


def fetch_smart_money_signal(
    market_id: str,
    top_traders: list[TopTrader],
    *,
    http: HttpClient | None = None,
    workspace_root: Path | None = None,
    seen: set[tuple[str, str]] | None = None,
    min_position_usdc: float = SM_MIN_POSITION_USDC,
    log_path: Path | None = None,
) -> SmartMoneySignal:
    """Aggregate top-trader positions in `market_id` (condition_id) into a signal.

    Logs the resulting signal to data/smart_money.jsonl (one row per call).
    Never raises.
    """
    seen_local = seen if seen is not None else set()

    pairs: list[tuple[TopTrader, TraderPosition]] = []
    for trader in top_traders:
        position = fetch_trader_position(
            trader.address,
            market_id,
            http=http,
            seen=seen_local,
            min_position_usdc=min_position_usdc,
        )
        if position is not None:
            pairs.append((trader, position))

    yes_weight = sum(t.weight * p.size_usdc for t, p in pairs if p.side == "YES")
    no_weight = sum(t.weight * p.size_usdc for t, p in pairs if p.side == "NO")
    total_weight = yes_weight + no_weight
    weighted_yes_prob = (yes_weight / total_weight) if total_weight > 0 else 0.5

    total_usdc = sum(p.size_usdc for _, p in pairs)
    largest = max((p.size_usdc for _, p in pairs), default=0.0)
    recent_24h = sum(1 for _, p in pairs if p.is_recent)

    signal = SmartMoneySignal(
        market_id=market_id,
        traders_present=len(pairs),
        weighted_yes_prob=round(weighted_yes_prob, 4),
        consensus_strength=_consensus_strength(len(pairs), total_usdc),
        largest_position_usdc=round(largest, 2),
        recent_entries_24h=recent_24h,
        total_smart_money_usdc=round(total_usdc, 2),
        top_trader_sides=[p.side for _, p in pairs],
        fetched_at=_utc_now_iso(),
        cache_hit=False,
    )

    target_log = log_path or _signal_log_path(workspace_root)
    try:
        target_log.parent.mkdir(parents=True, exist_ok=True)
        with target_log.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "timestamp": signal.fetched_at,
                        "market_id": signal.market_id,
                        "traders_present": signal.traders_present,
                        "weighted_yes_prob": signal.weighted_yes_prob,
                        "consensus_strength": signal.consensus_strength,
                        "largest_position_usdc": signal.largest_position_usdc,
                        "recent_entries_24h": signal.recent_entries_24h,
                        "total_smart_money_usdc": signal.total_smart_money_usdc,
                        "cache_hit": signal.cache_hit,
                    }
                )
                + "\n"
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("smart_money_log_write_failed error={}", exc)

    return signal


# --- apply_smart_money_modifier ----------------------------------------------


def apply_smart_money_modifier(result: Any, signal: SmartMoneySignal | None) -> Any:
    """Mutate-and-return the analyst result with a smart-money adjustment.

    The result is expected to be an AnalysisResult (a frozen dataclass in
    claude_analyst). Since it's immutable, we return a new instance.
    Returns the original when there's nothing to do.
    """
    if signal is None or signal.consensus_strength == "Low" or signal.traders_present == 0:
        return result

    # Smart-money implied direction
    if signal.weighted_yes_prob > 0.55:
        sm_dir = "BUY"
    elif signal.weighted_yes_prob < 0.45:
        sm_dir = "SELL"
    else:
        sm_dir = "NEUTRAL"

    decision = (getattr(result, "decision", "") or "").upper()
    # Map YES/NO to BUY/SELL for comparison
    if decision == "YES":
        decision = "BUY"
    elif decision == "NO":
        decision = "SELL"

    agrees = (decision == "BUY" and sm_dir == "BUY") or (decision == "SELL" and sm_dir == "SELL")
    contradicts = (decision == "BUY" and sm_dir == "SELL") or (decision == "SELL" and sm_dir == "BUY")

    if signal.consensus_strength == "High" and agrees:
        edge_mult = 1.20
        tag = " [SM:HIGH+AGREE→edge×1.20]"
        new_decision = result.decision
        logger.info(
            "smart_money_boost market={} direction={} edge_mult={}",
            signal.market_id, sm_dir, edge_mult,
        )
    elif signal.consensus_strength == "High" and contradicts:
        edge_mult = 1.0
        tag = " [SM:HIGH+CONTRADICT→SKIP]"
        new_decision = "SKIP"
        logger.warning(
            "smart_money_override market={} model_decision={} sm_direction={}",
            signal.market_id, result.decision, sm_dir,
        )
    elif signal.consensus_strength == "Medium" and agrees:
        edge_mult = 1.08
        tag = " [SM:MED+AGREE→edge×1.08]"
        new_decision = result.decision
    else:
        # Medium contradict / neutral → no modification but tag for audit.
        return result

    # AnalysisResult is frozen; build a new one with the adjusted edge/decision.
    new_edge = round(getattr(result, "edge", 0.0) * edge_mult, 6)
    new_reasoning = (getattr(result, "reasoning", "") + tag)[:200]
    try:
        return result.__class__(
            probability=result.probability,
            decision=new_decision,
            confidence=result.confidence,
            reasoning=new_reasoning,
            edge=new_edge,
            cost_usd=getattr(result, "cost_usd", 0.0),
            data_sources_used=list(getattr(result, "data_sources_used", []) or []),
            provider=getattr(result, "provider", "unknown"),
        )
    except TypeError:
        # Fallback for variant constructors — only mutate edge/reasoning if mutable.
        return result


__all__ = [
    "TopTrader",
    "TraderPosition",
    "SmartMoneySignal",
    "fetch_top_traders",
    "fetch_trader_position",
    "fetch_smart_money_signal",
    "apply_smart_money_modifier",
]
