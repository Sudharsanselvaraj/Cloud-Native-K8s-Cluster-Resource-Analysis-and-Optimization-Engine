#!/usr/bin/env python3
"""
CLI entrypoint for the K8s Resource Optimization Engine.

Usage:
  python main.py --input metrics.json
  python main.py --input metrics.json --output recommendations.json
  python main.py --input metrics.json --all          # include non-overprovisioned too
  python main.py --serve                             # start FastAPI server
"""

from __future__ import annotations

import argparse
import json
import sys

from app.engine import DEFAULT_CONFIG, OptimizationConfig, analyze_workload, run_optimization
from app.parser import ParseError, parse_metrics


def _print_table(results) -> None:
    """Pretty-print results as a table to stdout."""
    if not results:
        print("\n✅  No overprovisioned workloads detected.")
        return

    print(f"\n{'─'*80}")
    print(f"{'DEPLOYMENT':<22} {'CPU REQ':>9} {'CPU REC':>9} {'MEM REQ':>9} {'MEM REC':>9}  REASON")
    print(f"{'─'*80}")
    for r in results:
        reason_short = r.reason[:36] + "…" if len(r.reason) > 37 else r.reason
        print(
            f"{r.deployment:<22} "
            f"{r.original_cpu_request:>7}m  "
            f"{r.recommended_cpu:>7}m  "
            f"{r.original_memory_request:>7}Mi "
            f"{r.recommended_memory:>7}Mi  "
            f"{reason_short}"
        )
        if r.warnings:
            for w in r.warnings:
                print(f"  ⚠️  {w}")
    print(f"{'─'*80}")
    print(f"  {len(results)} workload(s) flagged as overprovisioned.\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Kubernetes Resource Optimization Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", "-i", help="Path to JSON metrics file (or '-' for stdin)")
    parser.add_argument("--output", "-o", help="Write JSON recommendations to file")
    parser.add_argument("--all", action="store_true", help="Include all workloads, not just overprovisioned")
    parser.add_argument("--serve", action="store_true", help="Start FastAPI HTTP server")
    parser.add_argument("--port", type=int, default=8080, help="Port for --serve (default: 8080)")
    parser.add_argument("--safety-buffer", type=float, default=0.25, help="Safety buffer pct (default: 0.25)")
    args = parser.parse_args()

    # ── Server mode ──────────────────────────────────────────────────────────
    if args.serve:
        try:
            import uvicorn
        except ImportError:
            print("uvicorn not installed. Run: pip install uvicorn", file=sys.stderr)
            return 1
        uvicorn.run("app.api:app", host="0.0.0.0", port=args.port, reload=False)
        return 0

    # ── File / stdin mode ─────────────────────────────────────────────────────
    if not args.input:
        parser.print_help()
        return 1

    if args.input == "-":
        raw = sys.stdin.read()
    else:
        try:
            with open(args.input) as f:
                raw = f.read()
        except FileNotFoundError:
            print(f"File not found: {args.input}", file=sys.stderr)
            return 1

    try:
        workloads = parse_metrics(raw)
    except ParseError as exc:
        print(f"❌  Parse error: {exc}", file=sys.stderr)
        return 1

    config = OptimizationConfig(safety_buffer_pct=args.safety_buffer)

    if args.all:
        results = [analyze_workload(w, config) for w in workloads]
    else:
        results = run_optimization(workloads, config)

    _print_table(results)

    output_data = [r.to_dict() for r in results]

    if args.output:
        with open(args.output, "w") as f:
            json.dump(output_data, f, indent=2)
        print(f"Results written to {args.output}")
    else:
        print(json.dumps(output_data, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
