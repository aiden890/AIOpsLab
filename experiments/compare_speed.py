"""Compare reasoning effort speed: xhigh vs low.

Reads scores.csv from two experiments and compares TTA, steps, and tokens.

Usage:
    python experiments/compare_speed.py
    python experiments/compare_speed.py --low results/experiments/gpt5-low-original --xhigh results/experiments/gpt5-xhigh-original
"""

import argparse
import csv
from pathlib import Path


def load_scores(scores_path: str) -> list[dict]:
    rows = []
    with open(scores_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "problem_id": row["problem_id"],
                "dataset": row.get("dataset", ""),
                "steps": int(row["steps"]) if row["steps"] else 0,
                "TTA": float(row["TTA"]) if row["TTA"] else 0,
                "in_tokens": int(row["in_tokens"]) if row["in_tokens"] else 0,
                "out_tokens": int(row["out_tokens"]) if row["out_tokens"] else 0,
                "score": float(row["score"]) if row["score"] else 0,
            })
    return rows


def stats(rows: list[dict], key: str) -> dict:
    vals = [r[key] for r in rows]
    if not vals:
        return {"avg": 0, "min": 0, "max": 0, "total": 0, "n": 0}
    return {
        "avg": sum(vals) / len(vals),
        "min": min(vals),
        "max": max(vals),
        "total": sum(vals),
        "n": len(vals),
    }


def print_comparison(low_rows, xhigh_rows):
    print(f"{'':30s} {'LOW':>12s} {'XHIGH':>12s} {'DIFF':>12s}")
    print("=" * 70)

    for key, label, fmt in [
        ("TTA", "Avg TTA (sec)", ".1f"),
        ("steps", "Avg Steps", ".1f"),
        ("in_tokens", "Avg Input Tokens", ".0f"),
        ("out_tokens", "Avg Output Tokens", ".0f"),
        ("score", "Avg Score", ".3f"),
    ]:
        low_s = stats(low_rows, key)
        xhigh_s = stats(xhigh_rows, key)
        diff = xhigh_s["avg"] - low_s["avg"]
        pct = (diff / low_s["avg"] * 100) if low_s["avg"] else 0
        print(f"{label:30s} {low_s['avg']:>{fmt}} {xhigh_s['avg']:>{fmt}} {diff:>+{fmt}} ({pct:+.1f}%)")

    # Total time
    low_total = sum(r["TTA"] for r in low_rows)
    xhigh_total = sum(r["TTA"] for r in xhigh_rows)
    diff_total = xhigh_total - low_total
    print(f"\n{'Total TTA (min)':30s} {low_total/60:>12.1f} {xhigh_total/60:>12.1f} {diff_total/60:>+12.1f}")
    print(f"{'Problems':30s} {len(low_rows):>12d} {len(xhigh_rows):>12d}")

    # Per-dataset breakdown
    datasets = sorted(set(r["dataset"] for r in low_rows + xhigh_rows if r["dataset"]))
    if datasets:
        print(f"\n{'--- Per Dataset TTA ---':^70s}")
        print(f"{'Dataset':30s} {'LOW avg':>12s} {'XHIGH avg':>12s} {'DIFF':>12s}")
        print("-" * 70)
        for ds in datasets:
            l = [r for r in low_rows if r["dataset"] == ds]
            x = [r for r in xhigh_rows if r["dataset"] == ds]
            l_avg = sum(r["TTA"] for r in l) / len(l) if l else 0
            x_avg = sum(r["TTA"] for r in x) / len(x) if x else 0
            diff = x_avg - l_avg
            print(f"{ds:30s} {l_avg:>12.1f} {x_avg:>12.1f} {diff:>+12.1f}")

    # Per-problem comparison (matching problems only)
    low_by_pid = {r["problem_id"]: r for r in low_rows}
    xhigh_by_pid = {r["problem_id"]: r for r in xhigh_rows}
    common = sorted(set(low_by_pid) & set(xhigh_by_pid))

    if common:
        print(f"\n{'--- Per Problem (common) ---':^70s}")
        print(f"{'Problem':40s} {'LOW TTA':>8s} {'X TTA':>8s} {'L stp':>6s} {'X stp':>6s}")
        print("-" * 70)
        for pid in common:
            l = low_by_pid[pid]
            x = xhigh_by_pid[pid]
            print(f"{pid:40s} {l['TTA']:>8.1f} {x['TTA']:>8.1f} {l['steps']:>6d} {x['steps']:>6d}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare low vs xhigh speed")
    parser.add_argument("--low", default="results/experiments/gpt5-low-original",
                        help="Low experiment directory")
    parser.add_argument("--xhigh", default="results/experiments/gpt5-xhigh-original",
                        help="Xhigh experiment directory")
    args = parser.parse_args()

    low_path = Path(args.low) / "scores.csv"
    xhigh_path = Path(args.xhigh) / "scores.csv"

    if not low_path.exists():
        print(f"Not found: {low_path}")
        exit(1)
    if not xhigh_path.exists():
        print(f"Not found: {xhigh_path}")
        exit(1)

    low_rows = load_scores(str(low_path))
    xhigh_rows = load_scores(str(xhigh_path))

    print(f"Low:   {args.low}")
    print(f"Xhigh: {args.xhigh}")
    print()

    print_comparison(low_rows, xhigh_rows)
