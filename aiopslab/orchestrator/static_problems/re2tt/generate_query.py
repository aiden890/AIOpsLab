"""Generate query.csv for RE2-TT static dataset problems.

Each row in query.csv corresponds to one fault injection case:
  (service, fault_type, trial) → one RCA problem.

Usage:
    python generate_query.py --re2tt-root /path/to/data/RE2/RE2-TT
    python generate_query.py  # uses default path relative to AIOpsLab root
"""

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

# Fault type → human-readable reason
FAULT_REASON_MAP = {
    "cpu":    "cpu stress",
    "mem":    "memory stress",
    "delay":  "network delay",
    "loss":   "packet loss",
    "disk":   "disk I/O stress",
    "socket": "socket exhaustion",
}

_UTC = timezone.utc

QUERY_FIELDS = [
    "task_index",
    "case_dir",
    "service",
    "fault",
    "trial",
    "inject_time",
    "instruction",
    "scoring_points",
]


def _fmt_ts(ts: int) -> str:
    """Full UTC string for instructions (readable)."""
    return datetime.fromtimestamp(ts, tz=_UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_ts_eval(ts: int) -> str:
    """UTC string in the exact format the evaluator parses: YYYY-MM-DD HH:MM:SS (no tz suffix)."""
    return datetime.fromtimestamp(ts, tz=_UTC).strftime("%Y-%m-%d %H:%M:%S")


def generate_query_csv(re2tt_root: Path, output_path: Path):
    """Scan RE2-TT directory and write query.csv."""
    re2tt_root = Path(re2tt_root)
    if not re2tt_root.exists():
        print(f"Error: RE2-TT root not found: {re2tt_root}")
        sys.exit(1)

    rows = []

    # Each subdir is {service}_{fault}, e.g. ts-auth-service_cpu
    for case_group in sorted(re2tt_root.iterdir()):
        if not case_group.is_dir():
            continue
        name = case_group.name
        if "_" not in name:
            continue

        # Split on last "_" to handle service names with underscores
        # e.g. ts-auth-service_cpu → service=ts-auth-service, fault=cpu
        parts = name.rsplit("_", 1)
        if len(parts) != 2:
            continue
        service, fault = parts[0], parts[1]

        if fault not in FAULT_REASON_MAP:
            continue

        reason = FAULT_REASON_MAP[fault]

        # Each numeric trial subdir
        for trial_dir in sorted(case_group.iterdir()):
            if not trial_dir.is_dir() or not trial_dir.name.isdigit():
                continue
            trial = trial_dir.name

            inject_time_file = trial_dir / "inject_time.txt"
            if not inject_time_file.exists():
                print(f"  Warning: inject_time.txt not found in {trial_dir}, skipping")
                continue

            inject_time = int(inject_time_file.read_text().strip())
            inject_dt      = _fmt_ts(inject_time)       # human-readable with UTC suffix
            inject_dt_eval = _fmt_ts_eval(inject_time)  # evaluator format (no tz suffix)
            case_dir = f"{name}/{trial}"

            instruction = (
                f"An incident has occurred in the Train Ticket microservice system. "
                f"Analyze the available telemetry data (metrics, logs, and traces) "
                f"to identify: "
                f"(1) the exact UTC datetime when the fault started "
                f"(format: YYYY-MM-DD HH:MM:SS), "
                f"(2) the root cause service, and "
                f"(3) the fault type."
            )

            scoring_points = (
                f"The only root cause occurrence time is within 1 minutes "
                f"(i.e., <=1min) of {inject_dt_eval}\n"
                f"The only predicted root cause component is {service}\n"
                f"The only predicted root cause reason is {reason}"
            )

            rows.append({
                "task_index":     "task_rca",
                "case_dir":       case_dir,
                "service":        service,
                "fault":          fault,
                "trial":          trial,
                "inject_time":    inject_time,
                "instruction":    instruction,
                "scoring_points": scoring_points,
            })

    if not rows:
        print("Warning: No RE2-TT cases found. Check the path.")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=QUERY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Generated {len(rows)} cases → {output_path}")


def main():
    # Default paths relative to AIOpsLab project root
    script_dir = Path(__file__).resolve().parent
    aiopslab_root = script_dir.parent.parent.parent.parent  # aiopslab/orchestrator/static_problems/re2tt → root

    parser = argparse.ArgumentParser(description="Generate query.csv for RE2-TT dataset")
    parser.add_argument(
        "--re2tt-root",
        type=str,
        default=str(aiopslab_root.parent / "RCAEval" / "data" / "RE2" / "RE2-TT"),
        help="Path to RE2-TT root directory (default: ../RCAEval/data/RE2/RE2-TT)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(script_dir / "query.csv"),
        help="Output path for query.csv",
    )
    args = parser.parse_args()

    generate_query_csv(Path(args.re2tt_root), Path(args.output))


if __name__ == "__main__":
    main()
