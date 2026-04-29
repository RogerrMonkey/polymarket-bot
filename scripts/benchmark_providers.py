"""Side-by-side analyst-provider benchmark.

Runs the paper-loop pipeline twice, once per provider, and prints a
comparison table over the analyses written by each. Useful for picking
the primary slot in the chain when a new provider is added.

Usage:
    python scripts/benchmark_providers.py --cycles 5 --interval 10
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANALYSES = ROOT / "data" / "analyses.jsonl"


def _read_after(timestamp: str) -> list[dict]:
    """Return analyses with timestamp >= the given ISO string."""
    if not ANALYSES.exists():
        return []
    out: list[dict] = []
    for line in ANALYSES.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        ts = str(row.get("timestamp") or "")
        if ts >= timestamp:
            out.append(row)
    return out


def _summarise(rows: list[dict], provider_label: str) -> dict:
    if not rows:
        return {
            "provider": provider_label,
            "n": 0,
            "low_pct": None,
            "med_pct": None,
            "high_pct": None,
            "directional_pct": None,
            "avg_edge": None,
            "avg_reasoning_chars": None,
            "avg_thinking_chars": None,
        }
    n = len(rows)

    def conf_pct(level: str) -> float:
        c = sum(1 for r in rows if str(r.get("confidence") or "").lower() == level.lower())
        return round(100.0 * c / n, 1)

    directional = [r for r in rows if str(r.get("decision") or "").upper() in {"YES", "NO", "BUY", "SELL"}]
    directional_pct = round(100.0 * len(directional) / n, 1)
    edges = [float(r.get("edge") or 0.0) for r in directional]
    avg_edge = round(statistics.fmean(edges), 4) if edges else None

    reasoning_lengths = [len(str(r.get("reasoning") or "")) for r in rows]
    avg_reasoning = round(statistics.fmean(reasoning_lengths), 1) if reasoning_lengths else None

    # NVIDIA's reasoning prefix encodes "[think:Nc] ..." — extract Nc.
    thinking_chars: list[int] = []
    for r in rows:
        reasoning = str(r.get("reasoning") or "")
        if reasoning.startswith("[think:"):
            try:
                end = reasoning.index("c]")
                thinking_chars.append(int(reasoning[len("[think:"):end]))
            except (ValueError, IndexError):
                continue
    avg_thinking = round(statistics.fmean(thinking_chars), 1) if thinking_chars else None

    return {
        "provider": provider_label,
        "n": n,
        "low_pct": conf_pct("Low"),
        "med_pct": conf_pct("Medium"),
        "high_pct": conf_pct("High"),
        "directional_pct": directional_pct,
        "avg_edge": avg_edge,
        "avg_reasoning_chars": avg_reasoning,
        "avg_thinking_chars": avg_thinking,
    }


def _run_paper_loop(cycles: int, interval: int, env_overrides: dict[str, str]) -> tuple[str, int]:
    """Invoke `python -m prediction_bot paper-loop` with extra env vars.

    Returns (started_at_iso, return_code).
    """
    started = datetime.now(timezone.utc).isoformat()
    env = os.environ.copy()
    env.update(env_overrides)
    py = shutil.which("python") or sys.executable
    cmd = [
        py, "-m", "prediction_bot", "paper-loop",
        "--cycles", str(cycles),
        "--interval", str(interval),
    ]
    print(f"  $ ANALYST_PROVIDER={env_overrides.get('ANALYST_PROVIDER')} {' '.join(cmd)}")
    proc = subprocess.run(cmd, env=env, cwd=str(ROOT))
    return started, proc.returncode


def _verdict(nv: dict, gq: dict) -> str:
    """Pick the better provider on a coarse weighted score.

    +2 if directional_pct higher (more usable signal)
    +1 if avg_edge higher (better mispricing detection)
    +1 if low_pct LOWER (less noise)
    """
    if nv["n"] == 0 and gq["n"] == 0:
        return "INSUFFICIENT_DATA"
    if nv["n"] == 0:
        return "GROQ_BETTER (NVIDIA unavailable)"
    if gq["n"] == 0:
        return "NVIDIA_BETTER (Groq unavailable)"
    score = 0
    if (nv["directional_pct"] or 0) > (gq["directional_pct"] or 0):
        score += 2
    elif (nv["directional_pct"] or 0) < (gq["directional_pct"] or 0):
        score -= 2
    if (nv["avg_edge"] or 0) > (gq["avg_edge"] or 0):
        score += 1
    elif (nv["avg_edge"] or 0) < (gq["avg_edge"] or 0):
        score -= 1
    if (nv["low_pct"] or 0) < (gq["low_pct"] or 0):
        score += 1
    elif (nv["low_pct"] or 0) > (gq["low_pct"] or 0):
        score -= 1
    if score > 0:
        return "NVIDIA_BETTER"
    if score < 0:
        return "GROQ_BETTER"
    return "SIMILAR"


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 10 else f"{v:.1f}"
    return str(v)


def _print_table(nv: dict, gq: dict, cycles: int) -> None:
    print()
    print(f"Provider Benchmark ({cycles} cycles each)")
    print("=" * 60)
    print(f"  {'Metric':<26} {'NVIDIA R1':<14} {'Groq 70B':<14}")
    print("-" * 60)
    print(f"  {'Sample size (n)':<26} {_fmt(nv['n']):<14} {_fmt(gq['n']):<14}")
    print(f"  {'Low confidence %':<26} {_fmt(nv['low_pct']):<14} {_fmt(gq['low_pct']):<14}")
    print(f"  {'Medium confidence %':<26} {_fmt(nv['med_pct']):<14} {_fmt(gq['med_pct']):<14}")
    print(f"  {'High confidence %':<26} {_fmt(nv['high_pct']):<14} {_fmt(gq['high_pct']):<14}")
    print(f"  {'Directional calls %':<26} {_fmt(nv['directional_pct']):<14} {_fmt(gq['directional_pct']):<14}")
    print(f"  {'Avg edge':<26} {_fmt(nv['avg_edge']):<14} {_fmt(gq['avg_edge']):<14}")
    print(f"  {'Avg reasoning length':<26} {_fmt(nv['avg_reasoning_chars']):<14} {_fmt(gq['avg_reasoning_chars']):<14}")
    print(f"  {'Avg thinking length':<26} {_fmt(nv['avg_thinking_chars']):<14} {'N/A':<14}")
    print("=" * 60)
    verdict = _verdict(nv, gq)
    print(f"Verdict: {verdict}")
    if "NVIDIA_BETTER" in verdict:
        print("Recommendation: keep NVIDIA as primary in build_provider_chain")
    elif "GROQ_BETTER" in verdict:
        print("Recommendation: keep Groq as primary; investigate NVIDIA before switching")
    else:
        print("Recommendation: providers are similar — NVIDIA primary keeps reasoning logs")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()

    nvidia_key = os.getenv("NVIDIA_API_KEY", "").strip()
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key:
        print("ERROR: GROQ_API_KEY not set; benchmark needs Groq as the comparison baseline.")
        return 2
    if not nvidia_key:
        print("WARNING: NVIDIA_API_KEY not set — only Groq leg will produce data.")

    print("Benchmark NVIDIA leg")
    nv_started, _ = _run_paper_loop(
        cycles=args.cycles,
        interval=args.interval,
        env_overrides={"ANALYST_PROVIDER": "nvidia"},
    )
    nv_rows = [r for r in _read_after(nv_started) if str(r.get("provider") or "") == "nvidia"]

    print("\nBenchmark Groq leg")
    gq_started, _ = _run_paper_loop(
        cycles=args.cycles,
        interval=args.interval,
        env_overrides={"ANALYST_PROVIDER": "groq"},
    )
    gq_rows = [r for r in _read_after(gq_started) if str(r.get("provider") or "") == "groq"]

    nv = _summarise(nv_rows, "nvidia")
    gq = _summarise(gq_rows, "groq")
    _print_table(nv, gq, args.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
