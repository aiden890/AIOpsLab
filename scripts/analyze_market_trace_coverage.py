"""Analyze how well Market trace anomalies align with ground truth.

Usage:
    python scripts/analyze_market_trace_coverage.py
    python scripts/analyze_market_trace_coverage.py --dataset openrca_market_cloudbed1
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections import Counter
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent.parent

RECORD_CSV_BY_DATASET = {
    "openrca_market_cloudbed1": REPO_ROOT / "aiopslab-applications" / "static_dataset" / "openrca" / "Market" / "cloudbed-1" / "record.csv",
    "openrca_market_cloudbed2": REPO_ROOT / "aiopslab-applications" / "static_dataset" / "openrca" / "Market" / "cloudbed-2" / "record.csv",
    "openrca_market_cb1": REPO_ROOT / "aiopslab-applications" / "static_dataset" / "openrca" / "Market" / "cloudbed-1" / "record.csv",
    "openrca_market_cb2": REPO_ROOT / "aiopslab-applications" / "static_dataset" / "openrca" / "Market" / "cloudbed-2" / "record.csv",
}

PREFILTERED_DIR_BY_DATASET = {
    "openrca_market_cloudbed1": REPO_ROOT / "prefiltered_telemetry" / "openrca_market_cloudbed1",
    "openrca_market_cloudbed2": REPO_ROOT / "prefiltered_telemetry" / "openrca_market_cloudbed2",
    "openrca_market_cb1": REPO_ROOT / "prefiltered_telemetry" / "openrca_market_cloudbed1",
    "openrca_market_cb2": REPO_ROOT / "prefiltered_telemetry" / "openrca_market_cloudbed2",
}

NAMESPACE_BY_DATASET = {
    "openrca_market_cloudbed1": "static-market-cb1",
    "openrca_market_cloudbed2": "static-market-cb2",
    "openrca_market_cb1": "static-market-cb1",
    "openrca_market_cb2": "static-market-cb2",
}


def load_trace_module():
    path = REPO_ROOT / "scripts" / "test_rca_trace_paths_from_csv.py"
    spec = importlib.util.spec_from_file_location("trace_mod", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="openrca_market_cloudbed1")
    parser.add_argument("--mode", choices=["raw", "detector"], default="raw")
    parser.add_argument("--min-edge-volume", type=int, default=5)
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--output-json", type=Path, default=REPO_ROOT / "tmp" / "market_trace_gt_coverage.json")
    return parser.parse_args()


def own_component_p90_max(raw: pd.DataFrame, component: str) -> float | None:
    if "duration" not in raw.columns:
        return None
    ss = raw[raw["cmdb_id"].astype(str) == component].copy()
    if ss.empty:
        return None
    ss["duration"] = pd.to_numeric(ss["duration"], errors="coerce")
    ss["timestamp"] = pd.to_numeric(ss["timestamp"], errors="coerce")
    ss = ss.dropna(subset=["duration", "timestamp"])
    if ss.empty:
        return None
    ss["minute"] = pd.to_datetime(ss["timestamp"], unit="s", utc=True).dt.floor("1min")
    grp = ss.groupby("minute")["duration"].quantile(0.9)
    if grp.empty:
        return None
    return float(grp.max())


def classify_reason(reason: str) -> str:
    text = str(reason).lower()
    if "network" in text:
        return "network"
    if "cpu" in text:
        return "cpu"
    if "memory" in text:
        return "memory"
    if "disk" in text or "i/o" in text:
        return "disk_io"
    if "process termination" in text:
        return "process"
    return "other"


def main() -> int:
    args = parse_args()
    mod = load_trace_module()

    record_df = pd.read_csv(RECORD_CSV_BY_DATASET[args.dataset])
    base_dir = PREFILTERED_DIR_BY_DATASET[args.dataset]
    namespace = NAMESPACE_BY_DATASET[args.dataset]

    rows: list[dict] = []
    for task_dir in sorted([p for p in base_dir.iterdir() if p.is_dir() and p.name.startswith("task_")]):
        print(f"[scan] {task_dir.name}", flush=True)
        idx = int(task_dir.name.rsplit("-", 1)[1])
        gt = record_df.iloc[idx].to_dict()
        trace_path = task_dir / namespace / "traces" / "trace_span.csv"
        if not trace_path.exists():
            continue

        raw = pd.read_csv(trace_path, low_memory=False)
        df = mod.load_trace_csv(trace_path, window_minutes=args.window_minutes)
        gt_component = str(gt["component"])
        gt_reason = str(gt["reason"])
        nonzero_status_rows = int((raw["status_code"].astype(str) != "0").sum()) if "status_code" in raw.columns else 0
        trace_has_gt_component = bool((raw["cmdb_id"].astype(str) == gt_component).any())
        edge = mod.build_trace_edge_metric_frame(df)
        anomalies = []
        gt_component_on_anomalous_edge = False
        dominant_metrics: dict[str, int] = {}
        if args.mode == "detector":
            anomalies = mod.detect_trace_edge_anomalies(
                edge,
                dataset=args.dataset,
                sustain_buckets=2,
                min_edge_volume=args.min_edge_volume,
            )
            gt_component_on_anomalous_edge = any(
                a.get("caller") == gt_component or a.get("callee") == gt_component
                for a in anomalies
            )
            dominant_metrics = dict(Counter(a.get("dominant_metric") for a in anomalies))
        rows.append(
            {
                "task": task_dir.name,
                "gt_component": gt_component,
                "gt_reason": gt_reason,
                "reason_family": classify_reason(gt_reason),
                "trace_rows": int(len(raw)),
                "edge_rows": int(len(edge)),
                "trace_has_gt_component": trace_has_gt_component,
                "nonzero_status_rows": nonzero_status_rows,
                "anomaly_count": int(len(anomalies)),
                "gt_component_on_anomalous_edge": gt_component_on_anomalous_edge,
                "dominant_metrics": dominant_metrics,
                "own_p90_max": own_component_p90_max(raw, gt_component),
            }
        )

    out = pd.DataFrame(rows)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(out.to_json(orient="records", indent=2), encoding="utf-8")

    print(f"tasks analyzed: {len(out)}")
    if args.mode == "detector":
        print("\nreason_family x anomaly_count>0")
        print(pd.crosstab(out["reason_family"], out["anomaly_count"] > 0).to_string())
        print("\nreason_family x gt_component_on_anomalous_edge")
        print(pd.crosstab(out["reason_family"], out["gt_component_on_anomalous_edge"]).to_string())
        print("\nno-trace-anomaly tasks")
        print(
            out.loc[
                out["anomaly_count"] == 0,
                ["task", "gt_component", "gt_reason", "nonzero_status_rows", "trace_has_gt_component", "own_p90_max"],
            ].to_string(index=False)
        )
        print("\ntrace-anomaly tasks where GT component is not on anomalous edge")
        print(
            out.loc[
                (out["anomaly_count"] > 0) & (~out["gt_component_on_anomalous_edge"]),
                ["task", "gt_component", "gt_reason", "anomaly_count", "dominant_metrics", "nonzero_status_rows", "own_p90_max"],
            ].to_string(index=False)
        )
    else:
        print("\nreason_family raw trace coverage")
        summary = out.groupby("reason_family").agg(
            tasks=("task", "count"),
            gt_present_rate=("trace_has_gt_component", "mean"),
            mean_nonzero_status=("nonzero_status_rows", "mean"),
            median_nonzero_status=("nonzero_status_rows", "median"),
            mean_own_p90_max=("own_p90_max", "mean"),
        )
        print(summary.to_string())
        print("\nnetwork-family tasks")
        print(
            out.loc[
                out["reason_family"] == "network",
                ["task", "gt_component", "gt_reason", "nonzero_status_rows", "trace_has_gt_component", "own_p90_max"],
            ].to_string(index=False)
        )
    print(f"\nsaved {args.output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
