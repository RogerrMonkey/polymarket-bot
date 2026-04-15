from __future__ import annotations

import argparse
from pathlib import Path

from prediction_bot.alerting import dispatch_alerts, print_alert_dispatch
from prediction_bot.checklist import print_pre_live_report, run_pre_live_checklist, write_pre_live_report
from prediction_bot.clients.http import HttpClient
from prediction_bot.clients.polymarket import PolymarketClient
from prediction_bot.config import load_config
from prediction_bot.dashboard import run_dashboard
from prediction_bot.main_loop import run_paper_loop, run_paper_loop_scheduled
from prediction_bot.outcome_resolver import OutcomeResolver, print_resolution_report, write_resolution_report
from prediction_bot.paper_metrics import check_paper_gates, print_scorecard
from prediction_bot.pipeline.compliance import run_preflight
from prediction_bot.pipeline.runner import execute_scan_run
from prediction_bot.storage.prediction_store import PredictionStore
from prediction_bot.synthetic_replay import print_synthetic_replay_report, run_synthetic_replay, write_synthetic_replay_report
from prediction_bot.telemetry import build_alerts, build_telemetry_snapshot, build_trend_snapshot, print_telemetry
from prediction_bot.usdc_ops import print_usdc_report, run_usdc_operational_checks


def _print_scan_output(result_count: int, decisions_count: int, errors: list[str]) -> None:
    print(f"scan_candidates={result_count}")
    print(f"risk_decisions={decisions_count}")
    if errors:
        print("ingestion_errors:")
        for err in errors:
            print(f"  - {err}")


def run_scan(limit_per_venue: int, top_n_for_risk: int) -> int:
    config = load_config()
    result = execute_scan_run(
        config=config,
        limit_per_venue=limit_per_venue,
        top_n_for_risk=top_n_for_risk,
        workspace_root=Path(".").resolve(),
    )

    _print_scan_output(
        result_count=len(result.candidates),
        decisions_count=len(result.risk_decisions),
        errors=result.ingestion_errors,
    )
    if result.preflight is not None:
        print(
            "preflight="
            f"checked:{result.preflight.checked},"
            f"blocked:{result.preflight.blocked},"
            f"country:{result.preflight.country},"
            f"region:{result.preflight.region},"
            f"message:{result.preflight.message}"
        )
    print(f"stored_predictions={result.stored_predictions}")
    print(f"db_path={result.db_path}")
    print(
        "brier_metrics="
        f"samples:{result.brier_metrics.sample_count},"
        f"brier:{result.brier_metrics.brier_score},"
        f"rmse:{result.brier_metrics.rmse}"
    )

    print("top_candidates:")
    for idx, candidate in enumerate(result.candidates[:10], start=1):
        snap = candidate.snapshot
        print(
            f"  {idx}. venue={snap.venue} id={snap.market_id} score={candidate.opportunity_score:.4f} "
            f"yes={snap.yes_price} spread={snap.spread} volume={snap.volume} liquidity={snap.liquidity}"
        )

    print("risk_preview:")
    for idx, (candidate, decision) in enumerate(result.risk_decisions[:10], start=1):
        r = result.research_signals.get(candidate.snapshot.market_id)
        research_part = "none"
        if r is not None:
            research_part = f"sent={r.sentiment_score:.3f};conf={r.confidence:.3f};evidence={r.evidence_count}"
        print(
            f"  {idx}. id={candidate.snapshot.market_id} approved={decision.approved} "
            f"edge={decision.edge:.4f} size={decision.position_fraction:.4f} reasons={','.join(decision.reasons) or 'none'} "
            f"research={research_part}"
        )

    return 0


def run_preflight_command() -> int:
    config = load_config()
    http = HttpClient(
        timeout_seconds=config.runtime.request_timeout_seconds,
        user_agent=config.runtime.user_agent,
    )
    polymarket = PolymarketClient(http) if config.venue.enable_polymarket else None
    status = run_preflight(config=config, polymarket=polymarket)

    print(f"checked={status.checked}")
    print(f"blocked={status.blocked}")
    print(f"country={status.country}")
    print(f"region={status.region}")
    print(f"ip={status.ip}")
    print(f"message={status.message}")

    if config.compliance.enforce_geoblock_gate and status.blocked is True:
        return 1
    return 0


def run_verify_auth_command() -> int:
    try:
        from prediction_bot.auth import verify_auth
    except Exception as exc:  # noqa: BLE001
        print(f"verify_auth_import_failed={exc}")
        return 1

    return 0 if verify_auth() else 1


def run_metrics(limit: int) -> int:
    config = load_config()
    store = PredictionStore(config.storage.db_path)
    metrics = store.brier_metrics()

    print(f"db_path={config.storage.db_path}")
    print(f"brier_samples={metrics.sample_count}")
    print(f"brier_score={metrics.brier_score}")
    print(f"brier_rmse={metrics.rmse}")

    print("recent_predictions:")
    for row in store.recent_predictions(limit=limit):
        print(
            f"  id={row['id']} venue={row['venue']} market={row['market_id']} "
            f"p={row['calibrated_probability']:.4f} edge={row['edge']:.4f} approved={bool(row['approved'])} outcome={row['outcome']}"
        )
    return 0


def run_settle(prediction_id: int, outcome: float) -> int:
    config = load_config()
    store = PredictionStore(config.storage.db_path)
    ok = store.set_outcome(prediction_id=prediction_id, outcome=outcome)
    if not ok:
        print(f"prediction_not_found id={prediction_id}")
        return 1

    print(f"settled id={prediction_id} outcome={outcome}")
    metrics = store.brier_metrics()
    print(f"updated_brier_samples={metrics.sample_count} brier={metrics.brier_score} rmse={metrics.rmse}")
    return 0


def run_scorecard(check_gates: bool) -> int:
    config = load_config()
    root = Path(".").resolve()
    print_scorecard(workspace_root=root, db_path=config.storage.db_path)
    if not check_gates:
        return 0

    ready, failures = check_paper_gates(workspace_root=root, db_path=config.storage.db_path)
    print(f"ready_for_live={ready}")
    if failures:
        print("failing_gates:")
        for item in failures:
            print(f"  - {item}")
    return 0 if ready else 1


def run_loop_command(
    cycles: int,
    interval_seconds: int,
    limit_per_venue: int,
    top_n_for_risk: int,
    dry_run: bool,
    allow_live_without_checklist: bool,
    schedule_kind: str | None = None,
    schedule_time: str = "08:00",
) -> int:
    root = Path(".").resolve()
    if not dry_run:
        config = load_config()
        ready, checks = run_pre_live_checklist(workspace_root=root, db_path=config.storage.db_path)
        print_pre_live_report(checks, all_passed=ready)
        write_pre_live_report(workspace_root=root, checks=checks, all_passed=ready)
        if not ready and not allow_live_without_checklist:
            print("live_mode_blocked_by_checklist=true")
            print("hint=use --allow-live-without-checklist only for controlled manual override")
            return 1

    if schedule_kind:
        return run_paper_loop_scheduled(
            schedule_kind=schedule_kind,
            schedule_time=schedule_time,
            cycles=cycles,
            interval_seconds=interval_seconds,
            limit_per_venue=limit_per_venue,
            top_n_for_risk=top_n_for_risk,
            workspace_root=root,
            dry_run=dry_run,
        )

    return run_paper_loop(
        cycles=cycles,
        interval_seconds=interval_seconds,
        limit_per_venue=limit_per_venue,
        top_n_for_risk=top_n_for_risk,
        workspace_root=root,
        dry_run=dry_run,
    )


def run_dashboard_command(host: str, port: int) -> int:
    return run_dashboard(host=host, port=port, workspace_root=Path(".").resolve())


def run_usdc_check_command() -> int:
    report = run_usdc_operational_checks(Path(".").resolve())
    print_usdc_report(report)
    return 0 if report.ready else 1


def run_telemetry_command() -> int:
    root = Path(".").resolve()
    config = load_config()
    telemetry, alerts = _build_operational_alert_context(root, config)
    trends = build_trend_snapshot(workspace_root=root, db_path=config.storage.db_path)
    print_telemetry(telemetry, alerts, trends=trends)
    return 0


def _build_operational_alert_context(root: Path, config) -> tuple[dict, list[str]]:
    summary_total = 0
    summary_rejected = 0
    api_cost_usd = 0.0
    try:
        from prediction_bot.paper_metrics import daily_summary

        summary = daily_summary(workspace_root=root, db_path=config.storage.db_path)
        summary_total = int(summary.get("total_signals", 0))
        summary_rejected = int(summary.get("signals_rejected", 0))
        api_cost_usd = float(summary.get("api_cost_usd", 0.0))
    except Exception:  # noqa: BLE001
        pass

    rejection_rate = None
    if summary_total > 0:
        rejection_rate = summary_rejected / summary_total

    telemetry = build_telemetry_snapshot(root)
    usdc = run_usdc_operational_checks(root)

    status = run_preflight(
        config=config,
        polymarket=PolymarketClient(
            HttpClient(
                timeout_seconds=config.runtime.request_timeout_seconds,
                user_agent=config.runtime.user_agent,
            )
        )
        if config.venue.enable_polymarket
        else None,
    )
    alerts = build_alerts(
        telemetry=telemetry,
        preflight_blocked=bool(status.blocked),
        usdc_ready=usdc.ready,
        rejection_rate=rejection_rate,
        api_cost_usd=api_cost_usd,
    )
    return telemetry, alerts


def run_notify_alerts_command(force: bool) -> int:
    root = Path(".").resolve()
    config = load_config()
    _, alerts = _build_operational_alert_context(root, config)
    result = dispatch_alerts(workspace_root=root, alerts=alerts, source="cli_notify_alerts", force=force)
    print_alert_dispatch(result)
    if result.skipped:
        return 0
    return 0 if result.sent else 1


def run_news_check_command(limit: int) -> int:
    """Smoke test the CryptoPanic + GDELT news pipeline; print the top N headlines."""
    from prediction_bot.research.news_feed import (
        CryptoPanicFetcher,
        GDELTFetcher,
        _cryptopanic_token_missing,
    )

    config = load_config()
    http = HttpClient(
        timeout_seconds=config.runtime.request_timeout_seconds,
        user_agent=config.runtime.user_agent,
    )

    token = config.research.cryptopanic_api_token
    if _cryptopanic_token_missing(token):
        print("cryptopanic_token=missing")
        print("hint=set BOT_CRYPTOPANIC_API_TOKEN in .env to enable CryptoPanic ingestion")
    else:
        print(f"cryptopanic_token=present (len={len(token.strip())})")

    cp_items = []
    if not _cryptopanic_token_missing(token):
        cp_items = CryptoPanicFetcher(http=http, api_token=token).fetch_once(limit=limit)
        print(f"cryptopanic_fetched={len(cp_items)}")

    gdelt_items = GDELTFetcher(http=http, query=config.research.gdelt_query).fetch_once(limit=limit)
    print(f"gdelt_fetched={len(gdelt_items)}")

    print(f"top_{limit}_headlines:")
    combined = (cp_items + gdelt_items)[:limit]
    if not combined:
        print("  (no headlines)")
        return 1

    for idx, item in enumerate(combined, start=1):
        print(
            f"  {idx}. [{item.source}] relevance={item.relevance_score:.2f} "
            f"sentiment={item.sentiment} title={item.title[:120]}"
        )
    return 0


def run_prelive_checklist_command(write_report: bool) -> int:
    root = Path(".").resolve()
    config = load_config()
    ready, checks = run_pre_live_checklist(workspace_root=root, db_path=config.storage.db_path)
    print_pre_live_report(checks, all_passed=ready)
    if write_report:
        path = write_pre_live_report(workspace_root=root, checks=checks, all_passed=ready)
        print(f"report_path={path}")
    return 0 if ready else 1


def run_synthetic_replay_command(
    days: int,
    loops_per_day: int,
    candidates_per_loop: int,
    approve_rate: float,
    resolved_rate: float,
    scenario: str,
    seed: int,
) -> int:
    root = Path(".").resolve()
    config = load_config()
    report = run_synthetic_replay(
        workspace_root=root,
        db_path=config.storage.db_path,
        days=days,
        loops_per_day=loops_per_day,
        candidates_per_loop=candidates_per_loop,
        approve_rate=approve_rate,
        resolved_rate=resolved_rate,
        scenario=scenario,
        seed=seed,
        write_resolution_stub=True,
    )
    report_path = write_synthetic_replay_report(workspace_root=root, report=report)
    print_synthetic_replay_report(report)
    print(f"report_path={report_path}")
    print(f"db_path={config.storage.db_path}")
    print(f"stub_path={root / 'data' / 'resolution_stub.json'}")
    return 0


def run_resolve_outcomes_command(limit: int, dry_run: bool, stub_mode: bool, stub_path: str | None) -> int:
    root = Path(".").resolve()
    config = load_config()
    store = PredictionStore(config.storage.db_path)
    http = HttpClient(
        timeout_seconds=config.runtime.request_timeout_seconds,
        user_agent=config.runtime.user_agent,
    )

    resolver = OutcomeResolver(
        workspace_root=root,
        http=http,
        dry_run=dry_run,
        stub_mode=stub_mode,
        stub_path=Path(stub_path) if stub_path else None,
    )
    report = resolver.settle_unresolved_predictions(store=store, limit=limit)
    report_path = write_resolution_report(workspace_root=root, report=report)
    print_resolution_report(report)
    print(f"report_path={report_path}")
    print(f"db_path={config.storage.db_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prediction market bot CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Run one ingestion + scan + risk cycle")
    scan.add_argument("--limit-per-venue", type=int, default=120)
    scan.add_argument("--top-n-for-risk", type=int, default=15)

    metrics = sub.add_parser("metrics", help="Show persisted prediction metrics (Brier, RMSE)")
    metrics.add_argument("--limit", type=int, default=10)

    settle = sub.add_parser("settle", help="Set binary outcome (0 or 1) for a stored prediction")
    settle.add_argument("--id", type=int, required=True)
    settle.add_argument("--outcome", type=float, required=True)

    scorecard = sub.add_parser("scorecard", help="Show paper-trading telemetry summary")
    scorecard.add_argument("--check-gates", action="store_true", help="Evaluate paper-trading gates")

    loop = sub.add_parser("paper-loop", help="Run paper trading loop cycles")
    loop.add_argument("--cycles", type=int, default=1, help="Number of cycles (0 for infinite)")
    loop.add_argument("--interval-seconds", type=int, default=30)
    loop.add_argument("--limit-per-venue", type=int, default=120)
    loop.add_argument("--top-n-for-risk", type=int, default=5)
    loop.add_argument("--dry-run", action="store_true", default=True)
    loop.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    loop.add_argument(
        "--allow-live-without-checklist",
        action="store_true",
        help="Bypass checklist block when running with --no-dry-run (not recommended)",
    )
    loop.add_argument(
        "--schedule",
        choices=["daily"],
        default=None,
        help="Run paper-loop on a recurring schedule via APScheduler (currently: daily)",
    )
    loop.add_argument(
        "--time",
        dest="schedule_time",
        default="08:00",
        help="UTC HH:MM at which the scheduled run fires (default: 08:00)",
    )

    replay = sub.add_parser("replay-synthetic", help="Generate synthetic historical data for offline replay/backtesting")
    replay.add_argument("--days", type=int, default=14)
    replay.add_argument("--loops-per-day", type=int, default=12)
    replay.add_argument("--candidates-per-loop", type=int, default=5)
    replay.add_argument("--approve-rate", type=float, default=0.45)
    replay.add_argument("--resolved-rate", type=float, default=0.55)
    replay.add_argument("--scenario", choices=["default", "bull_trend", "chop", "event_shock"], default="default")
    replay.add_argument("--seed", type=int, default=7)

    resolver = sub.add_parser("resolve-outcomes", help="Resolve unsettled predictions via stub map or Polymarket API")
    resolver.add_argument("--limit", type=int, default=200)
    resolver.add_argument("--dry-run", action="store_true", default=True)
    resolver.add_argument("--no-dry-run", dest="dry_run", action="store_false")
    resolver.add_argument("--stub-mode", action="store_true", default=True)
    resolver.add_argument("--no-stub-mode", dest="stub_mode", action="store_false")
    resolver.add_argument("--stub-path", default=None)

    dash = sub.add_parser("serve-dashboard", help="Run local operations dashboard")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", type=int, default=8787)

    sub.add_parser("usdc-check", help="Run USDC on/off-ramp operational readiness checks")
    sub.add_parser("telemetry", help="Show operational telemetry and alert summary")
    notify = sub.add_parser("notify-alerts", help="Send current alerts to configured webhook")
    notify.add_argument("--force", action="store_true", help="Send even if alert signature was already sent")
    prelive = sub.add_parser("prelive-checklist", help="Run compact Phase 9 pre-live PASS/FAIL checklist")
    prelive.add_argument("--no-write-report", action="store_true", help="Do not persist checklist report JSON")

    sub.add_parser("preflight", help="Run Gate Zero geo/access check")
    sub.add_parser("verify-auth", help="Run authenticated CLOB credential checks")

    news = sub.add_parser("news-check", help="Smoke test the news pipeline; print top headlines")
    news.add_argument("--limit", type=int, default=3)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "scan":
        return run_scan(args.limit_per_venue, args.top_n_for_risk)
    if args.command == "metrics":
        return run_metrics(args.limit)
    if args.command == "settle":
        return run_settle(args.id, args.outcome)
    if args.command == "scorecard":
        return run_scorecard(args.check_gates)
    if args.command == "paper-loop":
        return run_loop_command(
            cycles=args.cycles,
            interval_seconds=args.interval_seconds,
            limit_per_venue=args.limit_per_venue,
            top_n_for_risk=args.top_n_for_risk,
            dry_run=args.dry_run,
            allow_live_without_checklist=args.allow_live_without_checklist,
            schedule_kind=args.schedule,
            schedule_time=args.schedule_time,
        )
    if args.command == "replay-synthetic":
        return run_synthetic_replay_command(
            days=args.days,
            loops_per_day=args.loops_per_day,
            candidates_per_loop=args.candidates_per_loop,
            approve_rate=args.approve_rate,
            resolved_rate=args.resolved_rate,
            scenario=args.scenario,
            seed=args.seed,
        )
    if args.command == "resolve-outcomes":
        return run_resolve_outcomes_command(
            limit=args.limit,
            dry_run=args.dry_run,
            stub_mode=args.stub_mode,
            stub_path=args.stub_path,
        )
    if args.command == "serve-dashboard":
        return run_dashboard_command(host=args.host, port=args.port)
    if args.command == "usdc-check":
        return run_usdc_check_command()
    if args.command == "telemetry":
        return run_telemetry_command()
    if args.command == "notify-alerts":
        return run_notify_alerts_command(force=args.force)
    if args.command == "prelive-checklist":
        return run_prelive_checklist_command(write_report=(not args.no_write_report))
    if args.command == "preflight":
        return run_preflight_command()
    if args.command == "verify-auth":
        return run_verify_auth_command()
    if args.command == "news-check":
        return run_news_check_command(limit=args.limit)

    parser.error("Unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
