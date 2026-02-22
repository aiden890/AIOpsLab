#!/usr/bin/env python3
"""Telemetry Ablation Experiment Runner (RCA Agent).

Runs run_rca_agent.py across datasets x telemetry conditions to measure
the impact of each telemetry type on RCA accuracy.

Container isolation: Each condition gets its own Docker container via
namespace suffixing (e.g., static-bank-no-log). This is handled by
StaticDataset.__init__() when --condition is passed.

Usage:
    # Run all experiments (11 effective, telecom_no_log skipped)
    python experiments/telemetry_ablation/run_ablation.py

    # Single dataset
    python experiments/telemetry_ablation/run_ablation.py --dataset bank

    # Single condition
    python experiments/telemetry_ablation/run_ablation.py --condition no_log

    # Single experiment
    python experiments/telemetry_ablation/run_ablation.py --dataset bank --condition all

    # Summary only (also saves results/RESULTS.md)
    python experiments/telemetry_ablation/run_ablation.py --summary-only

    # Force re-run completed experiments
    python experiments/telemetry_ablation/run_ablation.py --dataset bank --condition all --force
"""

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RCA_SCRIPT = PROJECT_ROOT / "clients" / "run_rca_agent.py"
RESULTS_BASE = Path(__file__).resolve().parent / "results"

# ---------------------------------------------------------------------------
# Dataset definitions
# ---------------------------------------------------------------------------
DATASETS = {
    "bank": {
        "rca_datasets": ["openrca_bank"],
        "available_telemetry": ["log", "metric", "trace"],
    },
    "telecom": {
        "rca_datasets": ["openrca_telecom"],
        "available_telemetry": ["metric", "trace"],  # NO log data
    },
    "market": {
        "rca_datasets": ["openrca_market_cb1", "openrca_market_cb2"],
        "available_telemetry": ["log", "metric", "trace"],
    },
}

# ---------------------------------------------------------------------------
# Telemetry conditions
# ---------------------------------------------------------------------------
CONDITIONS = {
    "all":       {"enable_log": True,  "enable_metric": True,  "enable_trace": True},
    "no_log":    {"enable_log": False, "enable_metric": True,  "enable_trace": True},
    "no_metric": {"enable_log": True,  "enable_metric": False, "enable_trace": True},
    "no_trace":  {"enable_log": True,  "enable_metric": True,  "enable_trace": False},
}

# Conditions to skip per dataset (removing a type that doesn't exist = same as "all")
SKIP_CONDITIONS = {
    "telecom": ["no_log"],  # Telecom has no log data → no_log == all
}


# ---------------------------------------------------------------------------
# Result collection
# ---------------------------------------------------------------------------
def collect_results(results_dir: Path) -> list[dict]:
    """Parse all result JSON files in a results directory."""
    results = []
    for json_file in sorted(results_dir.rglob("*.json")):
        if json_file.name == "metadata.json":
            continue
        try:
            with open(json_file) as f:
                data = json.load(f)
            r = data.get("results", {})
            results.append({
                "problem_id": data.get("problem_id", ""),
                "task_type": r.get("task_type", ""),
                "difficulty": r.get("difficulty", ""),
                "score": r.get("score", 0.0),
                "success": r.get("success", False),
                "steps": r.get("steps", 0),
                "TTA": r.get("TTA", 0.0),
                "in_tokens": r.get("in_tokens", 0),
                "out_tokens": r.get("out_tokens", 0),
            })
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  Warning: could not parse {json_file.name}: {e}")
    return results


def is_experiment_done(eval_id: str) -> bool:
    """Check if an experiment has already completed (has result JSON files)."""
    result_dir = RESULTS_BASE / eval_id
    if not result_dir.exists():
        return False
    json_files = [f for f in result_dir.rglob("*.json") if f.name != "metadata.json"]
    return len(json_files) > 0


# ---------------------------------------------------------------------------
# Run a single experiment
# ---------------------------------------------------------------------------
def run_experiment(dataset_name: str, condition_name: str, max_steps: int = 25):
    """Run one dataset x condition experiment."""
    eval_id = f"{dataset_name}_{condition_name}"
    ds = DATASETS[dataset_name]
    flags = CONDITIONS[condition_name]

    print(f"\n{'=' * 70}")
    print(f"  Experiment: {eval_id}")
    print(f"  Telemetry: {flags}")
    print(f"{'=' * 70}\n")

    result_dir = RESULTS_BASE / eval_id
    result_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Write metadata
        metadata = {
            "eval_id": eval_id,
            "dataset": dataset_name,
            "condition": condition_name,
            "agent": "rca_agent",
            "telemetry": flags,
            "rca_datasets": ds["rca_datasets"],
            "max_steps": max_steps,
            "started_at": datetime.now().isoformat(),
        }
        with open(result_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)
            f.write("\n")

        # Run for each sub-dataset
        for rca_ds in ds["rca_datasets"]:
            print(f"\n--- Running dataset: {rca_ds} ---\n")
            work_dir = tempfile.mkdtemp(prefix=f"ablation_{eval_id}_{rca_ds}_")
            cmd = [
                sys.executable, str(RCA_SCRIPT),
                "--dataset", rca_ds,
                "--max-steps", str(max_steps),
                "--results-dir", str(result_dir),
                "--condition", condition_name,
                "--work-dir", work_dir,
            ]
            subprocess.run(cmd, cwd=str(PROJECT_ROOT))

        # Update metadata with completion time
        metadata["completed_at"] = datetime.now().isoformat()
        elapsed = (datetime.fromisoformat(metadata["completed_at"])
                   - datetime.fromisoformat(metadata["started_at"]))
        metadata["elapsed_minutes"] = round(elapsed.total_seconds() / 60, 2)
        with open(result_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)
            f.write("\n")

    finally:
        pass

    # Collect and summarize results
    results = collect_results(result_dir)
    if results:
        n = len(results)
        avg_score = sum(r["score"] for r in results) / n
        success_count = sum(1 for r in results if r["success"])
        print(f"\n  {eval_id}: {n} problems, avg_score={avg_score:.4f}, "
              f"success_rate={success_count}/{n} ({100*success_count/n:.1f}%)")
    else:
        print(f"\n  {eval_id}: No results collected.")

    return results


# ---------------------------------------------------------------------------
# Summary / RESULTS.md generation
# ---------------------------------------------------------------------------
def generate_summary():
    """Generate a summary table from all completed experiments."""
    all_experiments = []

    for ds_name in DATASETS:
        for cond_name in CONDITIONS:
            if cond_name in SKIP_CONDITIONS.get(ds_name, []):
                all_experiments.append({
                    "eval_id": f"{ds_name}_{cond_name}",
                    "dataset": ds_name,
                    "condition": cond_name,
                    "n": 0,
                    "avg_score": None,
                    "success_rate": None,
                    "status": "N/A",
                })
                continue
            eval_id = f"{ds_name}_{cond_name}"
            result_dir = RESULTS_BASE / eval_id
            results = collect_results(result_dir) if result_dir.exists() else []
            if not results:
                all_experiments.append({
                    "eval_id": eval_id,
                    "dataset": ds_name,
                    "condition": cond_name,
                    "n": 0,
                    "avg_score": None,
                    "success_rate": None,
                    "status": "Pending",
                })
                continue

            n = len(results)
            avg_score = sum(r["score"] for r in results) / n
            success_count = sum(1 for r in results if r["success"])
            all_experiments.append({
                "eval_id": eval_id,
                "dataset": ds_name,
                "condition": cond_name,
                "n": n,
                "avg_score": avg_score,
                "success_rate": f"{100*success_count/n:.1f}% ({success_count}/{n})",
                "status": "Done",
            })

    # Count completed (excluding N/A)
    done = sum(1 for e in all_experiments if e["status"] == "Done")
    total = sum(1 for e in all_experiments if e["status"] != "N/A")

    lines = []
    lines.append("# Telemetry Ablation - Results (RCA Agent)\n")
    lines.append(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    # Progress table
    lines.append(f"## Progress ({done}/{total})\n")
    lines.append("| # | Eval ID | Status | Problems | Avg Score | Success Rate |")
    lines.append("|---|---------|--------|----------|-----------|--------------|")
    for i, e in enumerate(all_experiments, 1):
        if e["status"] == "Done":
            lines.append(f"| {i} | `{e['eval_id']}` | Done | {e['n']} | "
                         f"{e['avg_score']:.4f} | {e['success_rate']} |")
        elif e["status"] == "N/A":
            lines.append(f"| {i} | `{e['eval_id']}` | N/A (redundant) | - | - | - |")
        else:
            lines.append(f"| {i} | `{e['eval_id']}` | Pending | - | - | - |")

    # Pivot tables
    for metric_name, metric_key in [("Avg Score", "avg_score"), ("Success Rate", "success_rate")]:
        lines.append(f"\n## {metric_name} Comparison\n")
        lines.append("| Dataset | all | no_log | no_metric | no_trace |")
        lines.append("|---------|------|------|------|------|")
        for ds_name in DATASETS:
            row = [ds_name]
            for cond_name in CONDITIONS:
                match = [e for e in all_experiments
                         if e["dataset"] == ds_name and e["condition"] == cond_name]
                if match and match[0]["status"] == "N/A":
                    row.append("N/A")
                elif match and match[0]["status"] == "Done":
                    val = match[0][metric_key]
                    if isinstance(val, float):
                        row.append(f"{val:.4f}")
                    else:
                        row.append(str(val))
                else:
                    row.append("-")
            lines.append("| " + " | ".join(row) + " |")

    md_text = "\n".join(lines) + "\n"

    # Write to file
    RESULTS_BASE.mkdir(parents=True, exist_ok=True)
    results_md = RESULTS_BASE / "RESULTS.md"
    with open(results_md, "w") as f:
        f.write(md_text)

    print(md_text)
    print(f"\nSaved to {results_md}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Telemetry Ablation Experiment Runner")
    parser.add_argument("--dataset", type=str, choices=list(DATASETS.keys()),
                        help="Run only this dataset")
    parser.add_argument("--condition", type=str, choices=list(CONDITIONS.keys()),
                        help="Run only this condition")
    parser.add_argument("--max-steps", type=int, default=25,
                        help="Max orchestrator steps per problem")
    parser.add_argument("--force", action="store_true",
                        help="Force re-run even if results exist")
    parser.add_argument("--summary-only", action="store_true",
                        help="Only print/save summary, don't run experiments")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.summary_only:
        generate_summary()
        return

    # Determine which experiments to run
    datasets = [args.dataset] if args.dataset else list(DATASETS.keys())
    conditions = [args.condition] if args.condition else list(CONDITIONS.keys())

    experiments = [
        (ds, cond) for ds in datasets for cond in conditions
        if cond not in SKIP_CONDITIONS.get(ds, [])
    ]

    print(f"Experiments planned: {len(experiments)}")
    for ds, cond in experiments:
        eval_id = f"{ds}_{cond}"
        done = is_experiment_done(eval_id)
        status = "DONE (skip)" if done and not args.force else "PENDING"
        print(f"  {eval_id}: {status}")

    # Show skipped conditions
    for ds in datasets:
        for skip_cond in SKIP_CONDITIONS.get(ds, []):
            if skip_cond in conditions:
                print(f"  {ds}_{skip_cond}: SKIPPED (redundant — "
                      f"'{skip_cond.replace('no_', '')}' not available in {ds})")

    for ds, cond in experiments:
        eval_id = f"{ds}_{cond}"
        if is_experiment_done(eval_id) and not args.force:
            print(f"\nSkipping {eval_id} (already done). Use --force to re-run.")
            continue
        run_experiment(ds, cond, max_steps=args.max_steps)

    # Always generate summary at the end
    generate_summary()


if __name__ == "__main__":
    main()
