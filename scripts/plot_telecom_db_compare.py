"""Plot Telecom DB metrics and latency for two DB components (e.g. db_007, db_009).

Usage example (from project root):

    python scripts/plot_telecom_db_compare.py \
        --metrics-csv aiopslab-applications/static_dataset/openrca/Telecom/telemetry/2020_05_21/metric/metric_service.csv \
        --traces-csv  aiopslab-applications/static_dataset/openrca/Telecom/telemetry/2020_05_21/trace/trace_span.csv \
        --db-ids db_007 db_009 \
        --start "2020-05-21 18:00:00" \
        --end   "2020-05-21 18:30:00" \
        --out-prefix telecom_db_007_009

This script is meant for ad-hoc analysis outside the pipeline to visually compare:
  - DB metrics (Sess_*, *_Read_Per_Sec, *_Write_Per_Sec, etc.)
  - Trace p95 latency to each DB
over the same time window.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Plot Telecom DB metrics + latency for two DBs.")
    p.add_argument("--metrics-csv", type=str, required=True, help="Path to DB metrics CSV (e.g. metric_service.csv).")
    p.add_argument("--traces-csv", type=str, required=True, help="Path to trace_span.csv.")
    p.add_argument(
        "--db-ids",
        type=str,
        nargs="+",
        default=["db_007", "db_009"],
        help="DB component ids to compare (e.g. db_007 db_009).",
    )
    p.add_argument(
        "--start",
        type=str,
        required=True,
        help='Start datetime (UTC), e.g. "2020-05-21 18:00:00".',
    )
    p.add_argument(
        "--end",
        type=str,
        required=True,
        help='End datetime (UTC), e.g. "2020-05-21 18:30:00".',
    )
    p.add_argument(
        "--out-prefix",
        type=str,
        default="telecom_db_compare",
        help="Output PNG prefix (files will be <prefix>_metrics.png and <prefix>_latency.png).",
    )
    return p.parse_args()


def _load_metrics(path: str, db_ids: list[str], start: str, end: str) -> pd.DataFrame:
    # low_memory=False to avoid mixed-type DtypeWarning on large files.
    df = pd.read_csv(path, low_memory=False)
    if "cmdb_id" not in df.columns:
        raise ValueError("metrics CSV must have cmdb_id column")
    if "timestamp" not in df.columns and "startTime" in df.columns:
        df = df.rename(columns={"startTime": "timestamp"})
    if "timestamp" not in df.columns:
        raise ValueError("metrics CSV must have timestamp or startTime column")

    ts = df["timestamp"].copy()
    # Handle ms vs s
    if ts.median() > 1e12:
        ts = ts / 1000.0
    df["datetime"] = pd.to_datetime(ts, unit="s", utc=True)

    # 1) Filter by time window; if that yields nothing (e.g. start/end mismatch with dataset),
    #    fall back to using the full time range so that at least something is plotted.
    mask = (df["datetime"] >= pd.Timestamp(start, tz="UTC")) & (
        df["datetime"] <= pd.Timestamp(end, tz="UTC")
    )
    df_window = df[mask]
    if df_window.empty:
        print(
            f"[metrics] No rows in time window {start}~{end}; "
            "falling back to full file time range."
        )
        df_window = df

    # 2) Filter by requested DB ids
    df_db = df_window[df_window["cmdb_id"].isin(db_ids)].copy()
    if df_db.empty:
        print(
            f"[metrics] No rows for requested DB ids {db_ids} "
            f"(available cmdb_id examples: {df_window['cmdb_id'].unique()[:10]})"
        )
    return df_db


def _load_traces(path: str, db_ids: list[str], start: str, end: str) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    # Normalize schema similar to _normalize_trace_schema
    if "cmdb_id" not in df.columns and "caller_cmdb_id" in df.columns:
        df = df.rename(columns={"caller_cmdb_id": "cmdb_id"})
    if "dsName" not in df.columns and "callee_cmdb_id" in df.columns:
        df = df.rename(columns={"callee_cmdb_id": "dsName"})
    if "startTime" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"startTime": "timestamp"})
    if "elapsedTime" not in df.columns and "duration" in df.columns:
        df = df.rename(columns={"duration": "elapsedTime"})

    if "dsName" not in df.columns:
        raise ValueError("trace_span.csv must have dsName column (callee DB id).")
    if "timestamp" not in df.columns:
        raise ValueError("trace_span.csv must have timestamp or startTime column.")

    ts = df["timestamp"].copy()
    # Telecom traces are in ms
    if ts.median() > 1e12:
        ts = ts / 1000.0
    df["datetime"] = pd.to_datetime(ts, unit="s", utc=True)

    mask = (df["datetime"] >= pd.Timestamp(start, tz="UTC")) & (
        df["datetime"] <= pd.Timestamp(end, tz="UTC")
    )
    df_window = df[mask]
    if df_window.empty:
        print(
            f"[traces] No rows in time window {start}~{end}; "
            "falling back to full file time range."
        )
        df_window = df

    df_db = df_window[df_window["dsName"].isin(db_ids)].copy()
    if df_db.empty:
        print(
            f"[traces] No rows for requested DB ids {db_ids} "
            f"(available dsName examples: {df_window['dsName'].unique()[:10]})"
        )
    return df_db


def plot_db_metrics(df: pd.DataFrame, db_ids: list[str], out_path: Path) -> None:
    if df.empty:
        print("No metric data after filtering; skipping metrics plot.")
        return

    # Pick key KPIs commonly used in Telecom DB analysis
    kpis = [
        "Sess_Active",
        "Sess_Connect",
        "Session_pct",
        "CPU_Used_Pct",
        "MEM_Used_Pct",
        "Logic_Read_Per_Sec",
        "Physical_Read_Per_Sec",
        "LFParaWrite_Per_Sec",
    ]
    present_kpis = [k for k in kpis if k in df["kpi_name"].unique()]
    if not present_kpis:
        print("None of the expected DB KPIs are present; skipping metrics plot.")
        return

    n = len(present_kpis)
    fig, axes = plt.subplots(n, 1, figsize=(14, 3.0 * n), sharex=True)
    if n == 1:
        axes = [axes]

    for ax, kpi in zip(axes, present_kpis):
        sub = df[df["kpi_name"] == kpi]
        for db in db_ids:
            s = sub[sub["cmdb_id"] == db].sort_values("datetime")
            if s.empty:
                continue
            ax.plot(
                s["datetime"],
                s["value"],
                label=db,
                linewidth=1.0,
                alpha=0.9,
            )
        ax.set_title(kpi, fontsize=11)
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.legend(loc="best", fontsize=8)

    axes[-1].set_xlabel("Time (UTC)")
    fig.suptitle(f"Telecom DB metrics: {', '.join(db_ids)}", fontsize=13)
    fig.tight_layout(rect=[0, 0.03, 1, 0.96])
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"Metrics plot saved to {out_path}")


def plot_db_latency(df_tr: pd.DataFrame, db_ids: list[str], out_path: Path) -> None:
    if df_tr.empty:
        print("No trace data after filtering; skipping latency plot.")
        return

    # Compute p50 / p95 latency per DB per minute
    df = df_tr.copy()
    df["bucket"] = df["datetime"].dt.floor("1min")

    fig, ax = plt.subplots(1, 1, figsize=(14, 4))
    for db in db_ids:
        sub = df[df["dsName"] == db]
        if sub.empty:
            continue
        grp = sub.groupby("bucket")["elapsedTime"]
        p95 = grp.quantile(0.95)
        ax.plot(
            p95.index,
            p95.values,
            label=f"{db} p95 latency",
            linewidth=1.0,
            alpha=0.9,
        )

    ax.set_title(f"Trace p95 latency: {', '.join(db_ids)}", fontsize=13)
    ax.set_ylabel("Elapsed Time (ms)")
    ax.set_xlabel("Time (UTC)")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.legend(loc="best", fontsize=9)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"Latency plot saved to {out_path}")


def main() -> None:
    args = _parse_args()
    db_ids = list(args.db_ids)
    out_prefix = Path(args.out_prefix)

    metrics_df = _load_metrics(args.metrics_csv, db_ids, args.start, args.end)
    traces_df = _load_traces(args.traces_csv, db_ids, args.start, args.end)

    # with_suffix() expects suffixes like ".png"; here we want to append
    # a custom suffix, so build the filenames manually.
    metrics_out = out_prefix.parent / f"{out_prefix.name}_metrics.png"
    latency_out = out_prefix.parent / f"{out_prefix.name}_latency.png"

    plot_db_metrics(metrics_df, db_ids, metrics_out)
    plot_db_latency(traces_df, db_ids, latency_out)


if __name__ == "__main__":
    main()

