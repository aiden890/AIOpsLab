"""Evaluate RCA accuracy from scores.csv with correct_component/reason/time columns.

Usage:
    python clients/eval_rca.py results/.../scores.csv
    python clients/eval_rca.py --time-threshold 3 results/.../scores.csv
    python clients/eval_rca.py --time-threshold 5 file1.csv file2.csv
"""

import sys
import csv
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict


def _pct(n, total):
    return f"{100 * n / total:.1f}%" if total else "N/A"


def _try_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _clip(text, width):
    s = str(text)
    if width <= 1:
        return s[:width]
    return s if len(s) <= width else s[: width - 1] + "…"


def _ratio_cell(n, total):
    return f"{n:>4}/{total:<4} ({_pct(n, total):>6})"


_TIME_FMTS = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]


def _signed_time_diff(row):
    """Compute signed time_diff_min = (pred_time - gt_time) in minutes.

    Falls back to the existing time_diff_min column if pred_time/gt_time are missing.
    Positive means prediction is AFTER ground truth.
    """
    pred_s = str(row.get("pred_time", "")).strip()
    gt_s = str(row.get("gt_time", "")).strip()
    if pred_s and gt_s:
        for fmt in _TIME_FMTS:
            try:
                t_pred = datetime.strptime(pred_s[:19], fmt).replace(tzinfo=timezone.utc)
                t_gt = datetime.strptime(gt_s[:19], fmt).replace(tzinfo=timezone.utc)
                return round((t_pred - t_gt).total_seconds() / 60.0, 1)
            except ValueError:
                continue
    # Fallback: use existing column (unsigned — treat as-is for backward compat)
    return _try_float(row.get("time_diff_min", ""))


def analyze(rows, time_threshold):
    total = len(rows)
    if not total:
        return

    # --- Component / Reason: direct from columns ---
    comp_correct = sum(1 for r in rows if str(r.get("correct_component", "")).strip() == "True")
    reason_correct = sum(1 for r in rows if str(r.get("correct_reason", "")).strip() == "True")

    # --- Time: re-evaluate with signed diff (pred - gt) ---
    time_correct = 0
    for r in rows:
        td = _signed_time_diff(r)
        if td is not None and 0 <= td <= time_threshold:
            time_correct += 1

    # Time diff stats
    diffs = [_signed_time_diff(r) for r in rows]
    diffs = [d for d in diffs if d is not None]

    time_label = f"Time(0~{time_threshold:.0f}m)"

    print(f"\n{'='*60}")
    print(f"  Total: {total}  |  time_threshold: {time_threshold:.0f} min")
    print(f"{'='*60}")
    print(f"\n  {'Metric':<14}  {'Correct':>8}  {'Total':>6}  {'Accuracy':>9}")
    print(f"  {'-'*44}")
    print(f"  {'Component':<14}  {comp_correct:>8}  {total:>6}  {_pct(comp_correct, total):>9}")
    print(f"  {'Reason':<14}  {reason_correct:>8}  {total:>6}  {_pct(reason_correct, total):>9}")
    print(f"  {time_label:<14}  {time_correct:>8}  {total:>6}  {_pct(time_correct, total):>9}")

    all_correct = sum(
        1 for r in rows
        if str(r.get("correct_component", "")).strip() == "True"
        and str(r.get("correct_reason", "")).strip() == "True"
        and (lambda td: td is not None and 0 <= td <= time_threshold )(_signed_time_diff(r))
    )
    print(f"  {'All correct':<14}  {all_correct:>8}  {total:>6}  {_pct(all_correct, total):>9}")

    if diffs:
        diffs_sorted = sorted(diffs)
        print(f"\n  Time diff (min): avg={sum(diffs)/len(diffs):.1f}, "
              f"median={diffs_sorted[len(diffs)//2]:.1f}, "
              f"min={diffs_sorted[0]:.1f}, max={diffs_sorted[-1]:.1f}")

    # --- By ground truth reason ---
    by_reason = defaultdict(lambda: {"total": 0, "comp": 0, "reason": 0, "time": 0, "all": 0})
    for r in rows:
        gt_r = r.get("gt_reason", "")
        if not gt_r:
            continue
        by_reason[gt_r]["total"] += 1
        comp_ok = str(r.get("correct_component", "")).strip() == "True"
        reason_ok = str(r.get("correct_reason", "")).strip() == "True"
        td = _signed_time_diff(r)
        time_ok = td is not None and 0 <= td <= time_threshold
        if comp_ok:
            by_reason[gt_r]["comp"] += 1
        if reason_ok:
            by_reason[gt_r]["reason"] += 1
        if time_ok:
            by_reason[gt_r]["time"] += 1
        if comp_ok and reason_ok and time_ok:
            by_reason[gt_r]["all"] += 1

    if by_reason:
        reason_w = max(20, min(48, max(len(str(r)) for r in by_reason.keys())))
        n_w = 4
        metric_w = 19
        line_w = 2 + reason_w + 2 + n_w + 2 + metric_w * 4 + 2
        print(f"\n  By ground truth reason:")
        print(
            f"  {'Reason':<{reason_w}}  {'N':>{n_w}}  "
            f"{'Comp':>{metric_w}}  {'Reason':>{metric_w}}  "
            f"{'Time':>{metric_w}}  {'All':>{metric_w}}"
        )
        print(f"  {'-' * line_w}")
        for reason in sorted(by_reason):
            d = by_reason[reason]
            t = d["total"]
            print(
                f"  {_clip(reason, reason_w):<{reason_w}}  {t:>{n_w}}  "
                f"{_ratio_cell(d['comp'], t):>{metric_w}}  "
                f"{_ratio_cell(d['reason'], t):>{metric_w}}  "
                f"{_ratio_cell(d['time'], t):>{metric_w}}  "
                f"{_ratio_cell(d['all'], t):>{metric_w}}"
            )

    # --- Per case ---
    print(f"\n  Per case:")
    print(f"  {'problem_id':<32} {'score':>5} {'comp':>5} {'reas':>5} {'time':>5} {'tdiff':>6}")
    print(f"  {'-'*66}")
    for r in rows:
        pid = r["problem_id"].replace("openrca_telecom-", "").replace("openrca_bank-", "")
        sc = r.get("score", "")
        comp = "O" if str(r.get("correct_component", "")).strip() == "True" else "X"
        reas = "O" if str(r.get("correct_reason", "")).strip() == "True" else "X"
        td = _signed_time_diff(r)
        time_mark = "O" if td is not None and 0 <= td <= time_threshold else "X"
        td_str = f"{td:+.1f}" if td is not None else "-"
        print(f"  {pid:<32} {sc:>5} {comp:>5} {reas:>5} {time_mark:>5} {td_str:>6}")

    print()


def main():
    parser = argparse.ArgumentParser(description="Evaluate RCA accuracy from scores.csv")
    parser.add_argument("files", nargs="+", help="scores.csv path(s)")
    parser.add_argument("--time-threshold", type=float, default=1.0,
                        help="Time correct if 0 <= (pred-gt) <= N minutes (default: 1)")
    args = parser.parse_args()

    all_rows = []
    for p in args.files:
        path = Path(p)
        if not path.exists():
            print(f"File not found: {path}")
            continue
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        print(f"\n{'━'*60}")
        print(f"  {path}")
        print(f"{'━'*60}")
        analyze(rows, args.time_threshold)
        all_rows.extend(rows)

    if len(args.files) > 1 and all_rows:
        print(f"\n{'━'*60}")
        print(f"  COMBINED ({len(args.files)} files)")
        print(f"{'━'*60}")
        analyze(all_rows, args.time_threshold)


if __name__ == "__main__":
    main()
