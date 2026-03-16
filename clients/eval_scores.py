"""Evaluate scores from a scores.csv file produced by the AIOpsLab runner.

Usage:
    python clients/eval_scores.py <path/to/scores.csv> [<path2> ...]
    python clients/eval_scores.py results/static_problems/openrca_telecom/react-rca/model/hypothesis-test-v11/scores.csv

Definitions:
    Full    : score == 1.0
    Partial : 0 < score < 1.0
    Fail    : score == 0.0
"""

import ast
import re
import sys
import csv
import glob as _glob
from pathlib import Path
from collections import defaultdict

# Which fields each task_type evaluates (OpenRCA telecom/bank)
TASK_EVAL_FIELDS = {
    "task_1": {"time"},
    "task_2": {"reason"},
    "task_3": {"component"},
    "task_4": {"time", "reason"},
    "task_5": {"time", "component"},
    "task_6": {"component", "reason"},
    "task_7": {"time", "component", "reason"},
}

# Known reason strings and component prefixes for classifying passing items
_REASONS = {
    "CPU fault", "network delay", "network loss",
    "db connection limit", "db close",
}
_COMP_PREFIXES = ("os_", "docker_", "db_", "redis_")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _classify_criteria(item: str):
    """Classify a passing/failing criteria item as 'component', 'reason', or 'time'."""
    item = item.strip()
    if item in _REASONS:
        return "reason"
    if any(item.startswith(p) for p in _COMP_PREFIXES):
        return "component"
    if _TIME_RE.match(item):
        return "time"
    return None


def _pct(n, total):
    return f"{100 * n / total:.1f}%" if total else "N/A"


def _bar(n, total, width=20):
    filled = round(width * n / total) if total else 0
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _parse_log_results(log_dir: Path, problem_id: str):
    """Parse passing_criteria/failing_criteria from a .log file."""
    # Find log file matching problem_id
    # problem_id like "openrca_telecom-task_2-0" → search for "*task_2-0*.log"
    parts = problem_id.rsplit("-", 2)
    if len(parts) >= 2:
        task_part = "-".join(parts[-2:])  # "task_2-0"
    else:
        task_part = problem_id

    # Search in log_dir and also in task subdirectory
    candidates = list(log_dir.glob(f"*{task_part}*.log"))
    task_subdir = log_dir / task_part
    if task_subdir.is_dir():
        candidates.extend(task_subdir.glob(f"*{task_part}*.log"))

    if not candidates:
        return None

    log_file = candidates[0]
    try:
        text = log_file.read_text(errors="replace")
    except Exception:
        return None

    # Find the results dict in the log (e.g., {'score': 0.33, 'passing_criteria': [...], ...})
    m = re.search(r"\{['\"]score['\"].*?passing_criteria.*?\}", text, re.DOTALL)
    if not m:
        return None
    try:
        result = ast.literal_eval(m.group(0))
        return result
    except Exception:
        return None


def _enrich_rows_from_logs(rows, csv_path: Path):
    """For rows missing correct_* fields, try to fill them from log files."""
    log_dir = csv_path.parent

    for row in rows:
        # Skip if already has new-format data
        if str(row.get("correct_component", "")).strip() in ("True", "False"):
            continue

        task_type = row.get("task_type", "")
        eval_fields = TASK_EVAL_FIELDS.get(task_type, set())
        if not eval_fields:
            continue

        score = float(row.get("score", 0))

        # For score 1.0 (all correct) or 0.0 (all wrong), no need to parse log
        if score == 1.0:
            for f in ("component", "reason", "time"):
                if f in eval_fields:
                    row[f"correct_{f}"] = "True"
            continue
        elif score == 0.0:
            for f in ("component", "reason", "time"):
                if f in eval_fields:
                    row[f"correct_{f}"] = "False"
            continue

        # Partial score — need log to determine which fields passed
        log_result = _parse_log_results(log_dir, row.get("problem_id", ""))
        if not log_result:
            continue

        passing = set()
        for item in log_result.get("passing_criteria", []):
            cls = _classify_criteria(str(item))
            if cls:
                passing.add(cls)

        for f in ("component", "reason", "time"):
            if f in eval_fields:
                row[f"correct_{f}"] = "True" if f in passing else "False"


def _field_correct(row, field):
    """Check if a specific field is correct for a row."""
    col = f"correct_{field}"
    val = str(row.get(col, "")).strip()

    if val == "True":
        return True
    elif val == "False":
        return False

    # Not evaluated for this task type
    task_type = row.get("task_type", "")
    if not _task_evaluates(task_type, field):
        return None

    # Still unknown
    return None


def _task_evaluates(task_type, field):
    """Check if a task_type evaluates a given field."""
    return field in TASK_EVAL_FIELDS.get(task_type, set())


def analyze(rows):
    total = len(rows)
    if total == 0:
        return

    full    = [r for r in rows if float(r["score"]) == 1.0]
    partial = [r for r in rows if 0.0 < float(r["score"]) < 1.0]
    fail    = [r for r in rows if float(r["score"]) == 0.0]
    mean_score = sum(float(r["score"]) for r in rows) / total

    print(f"\n{'='*52}")
    print(f"  Total tasks : {total}")
    print(f"  Mean score  : {mean_score:.3f}")
    print(f"{'='*52}")
    print(f"  {'Category':<10}  {'Count':>5}  {'Pct':>7}  Bar")
    print(f"  {'-'*46}")

    for label, subset in [("Full", full), ("Partial", partial), ("Fail", fail)]:
        n = len(subset)
        print(f"  {label:<10}  {n:>5}  {_pct(n, total):>7}  {_bar(n, total)}")

    # ---- by difficulty ----
    difficulties = sorted({r.get("difficulty", "?") for r in rows})
    if any(difficulties):
        print(f"\n  By difficulty:")
        print(f"  {'Diff':<10}  {'N':>4}  {'Full%':>7}  {'Partial%':>9}  {'Fail%':>7}  {'Mean':>6}")
        print(f"  {'-'*54}")
        for diff in difficulties:
            sub = [r for r in rows if r.get("difficulty") == diff]
            n = len(sub)
            f = sum(1 for r in sub if float(r["score"]) == 1.0)
            p = sum(1 for r in sub if 0.0 < float(r["score"]) < 1.0)
            fail_n = sum(1 for r in sub if float(r["score"]) == 0.0)
            mean = sum(float(r["score"]) for r in sub) / n
            print(f"  {diff:<10}  {n:>4}  {_pct(f, n):>7}  {_pct(p, n):>9}  {_pct(fail_n, n):>7}  {mean:>6.3f}")

    # ---- by task_type ----
    task_types = sorted({r.get("task_type", "?") for r in rows})
    if any(task_types):
        print(f"\n  By task_type:")
        print(f"  {'Type':<10}  {'N':>4}  {'Full%':>7}  {'Partial%':>9}  {'Fail%':>7}  {'Mean':>6}")
        print(f"  {'-'*54}")
        for tt in task_types:
            sub = [r for r in rows if r.get("task_type") == tt]
            n = len(sub)
            f = sum(1 for r in sub if float(r["score"]) == 1.0)
            p = sum(1 for r in sub if 0.0 < float(r["score"]) < 1.0)
            fail_n = sum(1 for r in sub if float(r["score"]) == 0.0)
            mean = sum(float(r["score"]) for r in sub) / n
            print(f"  {tt:<10}  {n:>4}  {_pct(f, n):>7}  {_pct(p, n):>9}  {_pct(fail_n, n):>7}  {mean:>6.3f}")

    print()


def analyze_rca(rows):
    """RCA-specific analysis: component / reason / time accuracy."""
    if not rows:
        return

    # --- Per-field accuracy ---
    results = {}
    for field in ("component", "reason", "time"):
        correct = 0
        wrong = 0
        not_eval = 0
        for r in rows:
            task_type = r.get("task_type", "")
            if not _task_evaluates(task_type, field):
                not_eval += 1
                continue
            result = _field_correct(r, field)
            if result is True:
                correct += 1
            elif result is False:
                wrong += 1
            else:
                wrong += 1  # unknown treated as wrong
        total_eval = correct + wrong
        results[field] = {"correct": correct, "wrong": wrong, "total_eval": total_eval}

    # Time diff stats
    time_diffs = []
    for r in rows:
        td = r.get("time_diff_min", "")
        if td and td != "":
            try:
                time_diffs.append(float(td))
            except ValueError:
                pass

    print(f"  {'─'*56}")
    print(f"  RCA Accuracy (per-field)")
    print(f"  {'─'*56}")
    print(f"  {'Metric':<12}  {'Correct':>8}  {'Total':>6}  {'Accuracy':>9}")
    print(f"  {'-'*42}")
    for field, label in [("component", "Component"), ("reason", "Reason"), ("time", "Time(<=1m)")]:
        d = results[field]
        n = d["total_eval"]
        acc = f"{100*d['correct']/n:.1f}%" if n else "N/A"
        print(f"  {label:<12}  {d['correct']:>8}  {n:>6}  {acc:>9}")

    if time_diffs:
        avg_diff = sum(time_diffs) / len(time_diffs)
        med_diff = sorted(time_diffs)[len(time_diffs) // 2]
        print(f"\n  Time diff (min): avg={avg_diff:.1f}, median={med_diff:.1f}, "
              f"min={min(time_diffs):.1f}, max={max(time_diffs):.1f}")

    # ---- By task_type ----
    task_types = sorted({r.get("task_type", "?") for r in rows})
    if task_types:
        print(f"\n  By task_type:")
        print(f"  {'Type':<10}  {'N':>4}  {'Comp':>12}  {'Reason':>12}  {'Time':>12}  {'AvgTdiff':>9}")
        print(f"  {'-'*68}")
        for tt in task_types:
            sub = [r for r in rows if r.get("task_type") == tt]
            n = len(sub)
            parts = []
            for field in ("component", "reason", "time"):
                if _task_evaluates(tt, field):
                    ok = sum(1 for r in sub if _field_correct(r, field) is True)
                    parts.append(f"{ok}/{n}")
                else:
                    parts.append("-")
            td_vals = []
            for r in sub:
                td = r.get("time_diff_min", "")
                if td:
                    try: td_vals.append(float(td))
                    except ValueError: pass
            td_str = f"{sum(td_vals)/len(td_vals):.1f}" if td_vals else "-"
            print(f"  {tt:<10}  {n:>4}  {parts[0]:>12}  {parts[1]:>12}  {parts[2]:>12}  {td_str:>9}")

    # ---- By ground truth reason ----
    by_reason = defaultdict(lambda: {"total": 0, "comp": 0, "reason": 0, "time": 0})
    for r in rows:
        gt_r = r.get("gt_reason", "")
        if not gt_r:
            continue
        by_reason[gt_r]["total"] += 1
        if _field_correct(r, "component") is True:
            by_reason[gt_r]["comp"] += 1
        if _field_correct(r, "reason") is True:
            by_reason[gt_r]["reason"] += 1
        if _field_correct(r, "time") is True:
            by_reason[gt_r]["time"] += 1

    if by_reason:
        print(f"\n  By ground truth reason:")
        print(f"  {'Reason':<20}  {'N':>4}  {'Comp':>10}  {'Reason':>10}  {'Time':>10}")
        print(f"  {'-'*60}")
        for reason in sorted(by_reason):
            d = by_reason[reason]
            t = d["total"]
            print(f"  {reason:<20}  {t:>4}  {d['comp']:>4}/{t:<4}  {d['reason']:>4}/{t:<4}  {d['time']:>4}/{t}")

    # ---- Per-case detail ----
    print(f"\n  Per case:")
    print(f"  {'problem_id':<32} {'score':>5} {'comp':>5} {'reas':>5} {'time':>5} {'tdiff':>6}")
    print(f"  {'-'*66}")
    for r in rows:
        pid = r["problem_id"].replace("openrca_telecom-", "").replace("openrca_bank-", "").replace("re2tt-", "")
        sc = r["score"]
        tt = r.get("task_type", "")
        marks = []
        for field in ("component", "reason", "time"):
            if not _task_evaluates(tt, field):
                marks.append("-")
            else:
                result = _field_correct(r, field)
                marks.append("O" if result is True else "X")
        td = r.get("time_diff_min", "")
        td_str = f"{float(td):.1f}" if td else "-"
        print(f"  {pid:<32} {sc:>5} {marks[0]:>5} {marks[1]:>5} {marks[2]:>5} {td_str:>6}")

    print()


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def main(paths):
    if not paths:
        print("Usage: python clients/eval_scores.py <scores.csv> [<scores2.csv> ...]")
        sys.exit(1)

    all_rows = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            print(f"File not found: {path}")
            continue
        rows = load_csv(path)

        # Enrich old-format rows from log files
        _enrich_rows_from_logs(rows, path)

        print(f"\n{'━'*52}")
        print(f"  {path}")
        print(f"{'━'*52}")
        analyze(rows)
        analyze_rca(rows)
        all_rows.extend(rows)

    if len(paths) > 1 and all_rows:
        print(f"\n{'━'*52}")
        print(f"  COMBINED ({len(paths)} files)")
        print(f"{'━'*52}")
        analyze(all_rows)
        analyze_rca(all_rows)


if __name__ == "__main__":
    main(sys.argv[1:])
