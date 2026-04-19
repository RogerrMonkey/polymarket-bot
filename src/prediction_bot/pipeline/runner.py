from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from prediction_bot.claude_analyst import ClaudeAnalyst
from prediction_bot.clients.http import HttpClient
from prediction_bot.clients.polymarket import PolymarketClient
from prediction_bot.config import AppConfig
from prediction_bot.core.calibration import ProbabilityCalibrator
from prediction_bot.core.scanner import MarketScanner
from prediction_bot.models import ResearchSignal, RiskDecision, ScanCandidate, TradeSignal
from prediction_bot.pipeline.compliance import PreflightStatus, run_preflight
from prediction_bot.pipeline.ingest import UnifiedIngestor
from prediction_bot.pipeline.research import ResearchPipeline
from prediction_bot.research.market_filter import filter_markets
from prediction_bot.research.news_feed import NewsItem, get_relevant_news
from prediction_bot.risk_engine import AnalysisResult as DeterministicAnalysisResult
from prediction_bot.risk_engine import PortfolioState, RiskConfig, pre_trade_check
from prediction_bot.storage.prediction_store import BrierMetrics, PredictionStore
from prediction_bot.watchlist import WatchlistManager


@dataclass
class ScanRunResult:
    candidates: list[ScanCandidate] = field(default_factory=list)
    risk_decisions: list[tuple[ScanCandidate, RiskDecision]] = field(default_factory=list)
    ingestion_errors: list[str] = field(default_factory=list)
    research_signals: dict[str, ResearchSignal] = field(default_factory=dict)
    stored_predictions: int = 0
    brier_metrics: BrierMetrics = field(default_factory=lambda: BrierMetrics(sample_count=0, brier_score=None, rmse=None))
    db_path: str = ""
    preflight: PreflightStatus | None = None


def _estimate_raw_model_probability(candidate: ScanCandidate, research_signal: ResearchSignal | None) -> float:
    # Transitional estimator: market baseline + scanner + research sentiment.
    # Phase 5 will replace this with calibrated model ensemble.
    market_prob = candidate.snapshot.yes_price or 0.5
    score_bias = max(-0.05, min(0.05, candidate.opportunity_score * 0.035))

    research_bias = 0.0
    if research_signal is not None:
        research_bias = research_signal.sentiment_score * research_signal.confidence * 0.08

    anomaly_penalty = 0.0
    if "wide_spread" in candidate.anomaly_flags:
        anomaly_penalty += 0.01
    if "very_wide_spread" in candidate.anomaly_flags:
        anomaly_penalty += 0.02

    estimated = market_prob + score_bias + research_bias - anomaly_penalty
    return max(0.01, min(0.99, estimated))


def _decision_from_probabilities(model_probability: float, market_probability: float) -> str:
    if abs(model_probability - market_probability) < 0.03:
        return "SKIP"
    return "YES" if model_probability > market_probability else "NO"


def _confidence_label(research_signal: ResearchSignal | None) -> str:
    if research_signal is None:
        return "Low"
    if research_signal.confidence >= 0.75:
        return "High"
    if research_signal.confidence >= 0.40:
        return "Medium"
    return "Low"


def _condition_id(snapshot: TradeSignal | ScanCandidate | object) -> str | None:
    raw = None
    if isinstance(snapshot, ScanCandidate):
        raw = snapshot.snapshot.raw
    elif hasattr(snapshot, "market") and hasattr(snapshot.market, "raw"):
        raw = snapshot.market.raw
    elif hasattr(snapshot, "raw"):
        raw = getattr(snapshot, "raw")

    if isinstance(raw, dict):
        value = raw.get("conditionId") or raw.get("condition_id")
        if value:
            return str(value)
    return None


def execute_scan_run(
    config: AppConfig,
    limit_per_venue: int = 150,
    top_n_for_risk: int = 15,
    workspace_root: Path | None = None,
) -> ScanRunResult:
    root = workspace_root or Path(".").resolve()

    http = HttpClient(
        timeout_seconds=config.runtime.request_timeout_seconds,
        user_agent=config.runtime.user_agent,
    )

    polymarket_client = PolymarketClient(http) if config.venue.enable_polymarket else None

    preflight = run_preflight(config=config, polymarket=polymarket_client)
    if config.compliance.enforce_geoblock_gate and preflight.blocked is True:
        return ScanRunResult(
            ingestion_errors=[
                "gate_zero_blocked:polymarket_geoblock",
                f"country:{preflight.country}",
                f"region:{preflight.region}",
            ],
            preflight=preflight,
            db_path=config.storage.db_path,
        )

    ingestor = UnifiedIngestor(
        polymarket=polymarket_client,
    )

    ingestion = ingestor.run(limit_per_venue=limit_per_venue)

    watchlist_manager = WatchlistManager(http=http, watchlist_path=root / "watchlist.json")
    watchlist = watchlist_manager.load_watchlist()
    if not watchlist:
        try:
            watchlist = watchlist_manager.refresh_with_btc_priority()
        except Exception:  # noqa: BLE001
            watchlist = []

    if watchlist:
        watchset = set(watchlist)
        filtered_snapshots = []
        for snap in ingestion.snapshots:
            cid = _condition_id(snap)
            if cid is None or cid in watchset:
                filtered_snapshots.append(snap)
        ingestion.snapshots = filtered_snapshots

    scanner = MarketScanner(config.scan)
    candidates = scanner.scan(ingestion.snapshots)

    # Pre-analyst quality filter: removes thin/expired/near-certain/malformed markets
    # so we don't waste Groq calls on markets with no analysable edge.
    risk_config = RiskConfig.from_json_file(root / "risk_config.json")
    candidates = filter_markets(candidates, risk_config)

    research_signals: dict[str, ResearchSignal] = {}
    if config.research.enabled and candidates:
        research = ResearchPipeline(
            settings=config.research,
            http=http,
            workspace_root=root,
        )
        market_subset = [c.snapshot for c in candidates[:top_n_for_risk]]
        research_signals = research.analyze_markets(market_subset)

    claude_analyst: ClaudeAnalyst | None = None
    claude_news_items: list[NewsItem] = []
    if config.claude.enabled:
        claude_analyst = ClaudeAnalyst(
            model=config.claude.model,
            web_search_max=config.claude.web_search_max,
            daily_budget_usd=config.claude.daily_budget_usd,
            log_path=root / "data" / "analyses.jsonl",
        )
        try:
            claude_news_items = get_relevant_news(
                http=http,
                gdelt_query=config.research.gdelt_query,
                min_relevance=config.research.news_min_relevance,
                max_age_minutes=config.research.news_max_age_minutes,
            )
        except Exception as exc:  # noqa: BLE001
            ingestion.errors.append(f"claude_news_error:{exc}")

    calibrator = ProbabilityCalibrator(config.calibration)

    portfolio = PortfolioState.from_json_file(
        root / "data" / "portfolio_state.json",
        default_starting_balance=100.0,
    )
    risk_log_path = root / "data" / "risk_log.jsonl"

    store = PredictionStore(config.storage.db_path)
    decisions: list[tuple[ScanCandidate, RiskDecision]] = []
    stored_predictions = 0

    for candidate in candidates[:top_n_for_risk]:
        market_prob = candidate.snapshot.yes_price
        if market_prob is None:
            continue
        research_signal = research_signals.get(candidate.snapshot.market_id)

        raw_model_probability = _estimate_raw_model_probability(candidate, research_signal)
        confidence_label = _confidence_label(research_signal)
        decision_label = _decision_from_probabilities(
            model_probability=raw_model_probability,
            market_probability=market_prob,
        )

        if claude_analyst is not None:
            llm_result = claude_analyst.analyze(
                market=candidate.snapshot,
                news_items=claude_news_items[:8],
                chainlink_price=None,
            )
            raw_model_probability = llm_result.probability
            confidence_label = llm_result.confidence
            decision_label = llm_result.decision

        calibrated = calibrator.calibrate(
            raw_probability=raw_model_probability,
            confidence=research_signal.confidence if research_signal is not None else 0.0,
            evidence_count=research_signal.evidence_count if research_signal is not None else 0,
            spread=candidate.snapshot.spread,
        )
        signal = TradeSignal(
            market=candidate.snapshot,
            model_probability=calibrated.calibrated_probability,
            market_probability=market_prob,
            research_signal=research_signal,
        )

        signed_edge = signal.edge
        if decision_label not in {"YES", "NO", "SKIP"}:
            decision_label = _decision_from_probabilities(
                model_probability=calibrated.calibrated_probability,
                market_probability=market_prob,
            )
        deterministic_analysis = DeterministicAnalysisResult(
            probability=calibrated.calibrated_probability,
            decision=decision_label,
            confidence=confidence_label,
            edge=abs(signed_edge),
            reasoning="scan_runner_deterministic_pretrade",
        )
        approved, reason, approved_size_usdc = pre_trade_check(
            analysis=deterministic_analysis,
            market=candidate.snapshot,
            portfolio=portfolio,
            config=risk_config,
            log_path=risk_log_path,
        )

        position_fraction = 0.0
        if portfolio.current_balance > 0:
            position_fraction = approved_size_usdc / portfolio.current_balance

        decision = RiskDecision(
            approved=approved,
            reasons=[] if approved else [reason],
            position_fraction=round(position_fraction, 6),
            edge=round(signed_edge, 6),
        )
        decisions.append((candidate, decision))

        try:
            store.record_prediction(
                venue=candidate.snapshot.venue,
                market_id=candidate.snapshot.market_id,
                question=candidate.snapshot.question,
                market_probability=market_prob,
                raw_model_probability=calibrated.raw_probability,
                calibrated_probability=calibrated.calibrated_probability,
                edge=decision.edge,
                approved=decision.approved,
                reasons=decision.reasons,
                opportunity_score=candidate.opportunity_score,
                research_sentiment=(research_signal.sentiment_score if research_signal is not None else None),
                research_confidence=(research_signal.confidence if research_signal is not None else None),
                research_evidence_count=(research_signal.evidence_count if research_signal is not None else None),
            )
            stored_predictions += 1
        except Exception as exc:  # noqa: BLE001
            ingestion.errors.append(f"prediction_store_error:{exc}")

    try:
        store.record_scan_run(
            candidate_count=len(candidates),
            decision_count=len(decisions),
            ingestion_errors=ingestion.errors,
        )
    except Exception as exc:  # noqa: BLE001
        ingestion.errors.append(f"scan_store_error:{exc}")

    brier_metrics = store.brier_metrics()

    return ScanRunResult(
        candidates=candidates,
        risk_decisions=decisions,
        ingestion_errors=ingestion.errors,
        research_signals=research_signals,
        stored_predictions=stored_predictions,
        brier_metrics=brier_metrics,
        db_path=config.storage.db_path,
        preflight=preflight,
    )
