from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from loguru import logger

from prediction_bot.clients.http import HttpClient
from prediction_bot.config import AppConfig, load_config
from prediction_bot.executor import OrderExecutor
from prediction_bot.outcome_resolver import OutcomeResolver, write_resolution_report
from prediction_bot.pipeline.runner import ScanRunResult, execute_scan_run
from prediction_bot.risk_engine import PortfolioState
from prediction_bot.scheduler_health import record_cycle_health
from prediction_bot.storage.prediction_store import PredictionStore
from prediction_bot.utils.network import check_warp_active


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def _run_lock_path(workspace_root: Path) -> Path:
    return workspace_root / "data" / "run.lock"


def _pid_is_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in (out.stdout or "")
        except Exception:  # noqa: BLE001
            # If we can't check, assume alive so we don't steal a lock from a live process.
            return True
    # POSIX
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return True


def _acquire_run_lock(workspace_root: Path, *, cycles_planned: int) -> bool:
    """Write data/run.lock with current pid/start/cycles.

    If a stale lock file references a dead PID, log WARNING, emit a
    scheduler_health row with status='crashed' for that previous date,
    then overwrite the lock and proceed. Returns True on success.
    """
    lock = _run_lock_path(workspace_root)
    if lock.exists():
        try:
            prev = json.loads(lock.read_text(encoding="utf-8"))
            prev_pid = int(prev.get("pid", 0) or 0)
        except Exception:  # noqa: BLE001
            prev_pid = 0
        if prev_pid and _pid_is_alive(prev_pid):
            logger.error("run_lock_conflict another_run_active pid={}", prev_pid)
            return False
        logger.warning(
            "run_lock_stale previous run may have crashed pid={} — recording crash and continuing",
            prev_pid,
        )
        try:
            record_cycle_health(
                workspace_root,
                status_override="crashed",
                reason_override=f"stale_lock_recovered pid={prev_pid}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler_health_crash_record_failed error={}", exc)

    lock.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "started_at": _utc_now_iso(),
        "cycles_planned": int(cycles_planned),
    }
    lock.write_text(json.dumps(payload), encoding="utf-8")
    return True


def _release_run_lock(workspace_root: Path) -> None:
    lock = _run_lock_path(workspace_root)
    try:
        if lock.exists():
            lock.unlink()
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_lock_release_failed error={}", exc)


def _append_heartbeat(workspace_root: Path, *, cycle: int, analyses_so_far: int) -> None:
    """Emit a per-cycle heartbeat row into scheduler_health.jsonl."""
    from prediction_bot.scheduler_health import scheduler_health_path

    path = scheduler_health_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "type": "heartbeat",
        "cycle": int(cycle),
        "analyses_so_far": int(analyses_so_far),
        "timestamp": _utc_now_iso(),
    }
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat_write_failed error={}", exc)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decision_side(edge: float) -> str:
    return "YES" if edge >= 0.0 else "NO"


def _run_cycle(
    config: AppConfig,
    workspace_root: Path,
    limit_per_venue: int,
    top_n_for_risk: int,
    executor: OrderExecutor,
) -> dict:
    result: ScanRunResult = execute_scan_run(
        config=config,
        limit_per_venue=limit_per_venue,
        top_n_for_risk=top_n_for_risk,
        workspace_root=workspace_root,
    )

    portfolio = PortfolioState.from_json_file(
        workspace_root / "data" / "portfolio_state.json",
        default_starting_balance=100.0,
    )

    approved_count = 0
    executed_count = 0

    for candidate, decision in result.risk_decisions[:top_n_for_risk]:
        if not decision.approved:
            continue
        approved_count += 1

        side = _decision_side(decision.edge)
        size_usdc = max(1.0, round(decision.position_fraction * portfolio.current_balance, 6))
        trade = executor.place_maker_order(candidate.snapshot, side, size_usdc)
        portfolio.record_fill(asdict(trade))
        executed_count += 1

    summary = {
        "timestamp": _utc_now_iso(),
        "scan_candidates": len(result.candidates),
        "risk_decisions": len(result.risk_decisions),
        "approved": approved_count,
        "executed": executed_count,
        "ingestion_errors": result.ingestion_errors,
    }
    _append_jsonl(workspace_root / "data" / "loop_log.jsonl", summary)
    return summary


def run_paper_loop(
    cycles: int,
    interval_seconds: int,
    limit_per_venue: int,
    top_n_for_risk: int,
    workspace_root: Path,
    dry_run: bool,
) -> int:
    # Env-level KILL_SWITCH is an operator panic button: abort before we even
    # spin up the executor so no side effects (order placement, cost, logs) occur.
    # File-level kill_switch in risk_config.json is enforced per-order in risk_engine.
    kill_env = (os.getenv("KILL_SWITCH") or "").strip().lower()
    if kill_env in {"1", "true", "yes", "on"}:
        print("kill_switch_env_active=true paper-loop aborted before first cycle")
        return 0

    # Startup WARP check + auto-connect. If WARP is inactive we attempt a
    # best-effort `warp-cli connect`, wait briefly, and re-check. Non-fatal:
    # we log WARNING and proceed either way so one bad morning does not lose
    # a paper day of analyses.
    warp_active = check_warp_active()
    warp_auto_connect_attempted = False
    if not warp_active:
        logger.warning(
            "WARP_INACTIVE — attempting warp-cli connect (5s settle). "
            "Enable Cloudflare WARP before scheduled run time for reliability."
        )
        warp_auto_connect_attempted = True
        try:
            subprocess.run(
                ["warp-cli", "connect"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            time.sleep(5)
            warp_active = check_warp_active()
            if warp_active:
                logger.info("warp_auto_connect_ok — WARP is now active")
            else:
                logger.warning("warp_auto_connect_failed — scanner may return 0 candidates")
        except FileNotFoundError:
            logger.warning(
                "warp_cli_not_found — install from https://1.1.1.1 and retry; "
                "proceeding without WARP"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("warp_auto_connect_error error={}", exc)

    # Crash-recovery lock file. Stale locks (dead PID) auto-clear with a
    # scheduler_health "crashed" row so operators can see what happened.
    if not _acquire_run_lock(workspace_root, cycles_planned=cycles):
        logger.error("paper_loop_abort reason=run_lock_held_by_live_pid")
        return 1

    config = load_config()
    executor = OrderExecutor(dry_run=dry_run, trades_path=workspace_root / "data" / "trades.jsonl")
    stop_requested = False
    analyses_so_far = 0

    def _handle_signal(signum, frame):  # noqa: ANN001, ARG001
        nonlocal stop_requested
        stop_requested = True
        print(f"signal_received={signum}")

    prev_handlers: dict[int, object] = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            prev_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _handle_signal)
        except Exception:  # noqa: BLE001
            continue

    try:
        iteration = 0
        while True:
            if stop_requested:
                break

            iteration += 1
            summary = _run_cycle(
                config=config,
                workspace_root=workspace_root,
                limit_per_venue=limit_per_venue,
                top_n_for_risk=top_n_for_risk,
                executor=executor,
            )
            print(
                f"loop_cycle={iteration} candidates={summary['scan_candidates']} "
                f"approved={summary['approved']} executed={summary['executed']}"
            )

            # Heartbeat: one row per completed cycle so partially-completed
            # runs (crash mid-way) still have a visible record in the health log.
            analyses_so_far += int(summary.get("risk_decisions") or 0)
            _append_heartbeat(workspace_root, cycle=iteration, analyses_so_far=analyses_so_far)

            if cycles > 0 and iteration >= cycles:
                break

            sleep_left = max(1, interval_seconds)
            while sleep_left > 0 and not stop_requested:
                time.sleep(1)
                sleep_left -= 1

    except KeyboardInterrupt:
        stop_requested = True
    finally:
        for sig, previous in prev_handlers.items():
            try:
                signal.signal(sig, previous)
            except Exception:  # noqa: BLE001
                continue

    if stop_requested:
        cancelled = executor.cancel_all_open_orders()
        print(f"cancelled_open_orders={cancelled}")
        # Best-effort health-record even on interrupted runs — operators need to
        # see that the day was disrupted, not pretend it didn't happen.
        try:
            record_cycle_health(
                workspace_root,
                warp_active=warp_active,
                warp_auto_connect_attempted=warp_auto_connect_attempted,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduler_health_write_failed error={}", exc)
        _release_run_lock(workspace_root)
        return 130

    # End-of-run outcome resolver hook. The full daily cycle is:
    #   scan → analyse → paper trade → resolve.
    # Resolver is stub+dry-run by default in paper mode (never crashes the loop).
    try:
        http = HttpClient(
            timeout_seconds=config.runtime.request_timeout_seconds,
            user_agent=config.runtime.user_agent,
        )
        resolver = OutcomeResolver(
            workspace_root=workspace_root,
            http=http,
            dry_run=dry_run,  # mirror loop's dry_run: paper mode = dry_run=True
            stub_mode=True,   # stub-map always consulted first
        )
        store = PredictionStore(config.storage.db_path)
        report = resolver.settle_unresolved_predictions(store=store, limit=200)
        write_resolution_report(workspace_root, report)
        logger.info(
            "paper_loop_resolver checked={} resolved={} applied={} unresolved={} errors={}",
            report.checked, report.resolved, report.applied, report.unresolved, report.errors,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("paper_loop_resolver_failed error={}", exc)

    # Record scheduler health so the dashboard and prelive-checklist can gate
    # live-mode readiness on demonstrated reliability.
    try:
        record_cycle_health(
            workspace_root,
            warp_active=warp_active,
            warp_auto_connect_attempted=warp_auto_connect_attempted,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler_health_write_failed error={}", exc)

    _release_run_lock(workspace_root)
    return 0


def _parse_schedule_time(value: str) -> tuple[int, int]:
    """Parse 'HH:MM' UTC time string. Raises ValueError on bad input."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time format (want HH:MM): {value}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"hour/minute out of range: {value}")
    return hour, minute


def _build_scheduled_job(
    *,
    cycles: int,
    interval_seconds: int,
    limit_per_venue: int,
    top_n_for_risk: int,
    workspace_root: Path,
    dry_run: bool,
) -> Callable[[], None]:
    """Build the daily-fire callable that runs one paper-loop pass."""

    def _job() -> None:
        run_at = datetime.now(timezone.utc).isoformat()
        logger.info(f"scheduled_paper_loop_started run_at_utc={run_at}")
        try:
            exit_code = run_paper_loop(
                cycles=cycles,
                interval_seconds=interval_seconds,
                limit_per_venue=limit_per_venue,
                top_n_for_risk=top_n_for_risk,
                workspace_root=workspace_root,
                dry_run=dry_run,
            )
            logger.info(f"scheduled_paper_loop_completed exit_code={exit_code}")
        except Exception as exc:  # noqa: BLE001
            # Never crash the scheduler — log and let the next firing try again.
            logger.exception(f"scheduled_paper_loop_failed error={exc}")

    return _job


def run_paper_loop_scheduled(
    *,
    schedule_kind: str,
    schedule_time: str,
    cycles: int,
    interval_seconds: int,
    limit_per_venue: int,
    top_n_for_risk: int,
    workspace_root: Path,
    dry_run: bool,
    scheduler_factory: Callable | None = None,
) -> int:
    """Run paper-loop on a recurring schedule using APScheduler.

    schedule_kind: currently only 'daily' (room to grow)
    schedule_time: 'HH:MM' UTC
    """
    if schedule_kind != "daily":
        raise ValueError(f"unsupported schedule kind: {schedule_kind}")

    hour, minute = _parse_schedule_time(schedule_time)

    if scheduler_factory is None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger

        def scheduler_factory():  # noqa: ANN202
            return AsyncIOScheduler(timezone="UTC"), CronTrigger
    scheduler, trigger_cls = scheduler_factory()

    job = _build_scheduled_job(
        cycles=cycles,
        interval_seconds=interval_seconds,
        limit_per_venue=limit_per_venue,
        top_n_for_risk=top_n_for_risk,
        workspace_root=workspace_root,
        dry_run=dry_run,
    )

    scheduler.add_job(
        job,
        trigger=trigger_cls(hour=hour, minute=minute, timezone="UTC"),
        id="paper_loop_daily",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    logger.info(
        f"scheduler_started kind={schedule_kind} fire_at_utc={hour:02d}:{minute:02d} dry_run={dry_run}"
    )
    print(f"scheduler_started kind={schedule_kind} fire_at_utc={hour:02d}:{minute:02d}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    scheduler.start()
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("scheduler_keyboard_interrupt — shutting down")
        print("scheduler_stopped_by_user")
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass
        try:
            loop.close()
        except Exception:  # noqa: BLE001
            pass

    return 0
