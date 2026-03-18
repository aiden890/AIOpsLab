#!/usr/bin/env python3
"""Visualize Market trace latency & error rate per component around a fault window.

Usage example:

  python scripts/visualize_trace_market.py \
    --trace-csv aiopslab-applications/static_dataset/openrca/Market/cloudbed-1/telemetry/2022_03_20/trace/trace_span.csv \
    --component cartservice \
    --reason "container network latency" \
    --fault-local "2022-03-20 21:16:21" \
    --output-dir results/trace_viz_market/cartservice_20220320_211621

Notes:
  - trace_span.csv has timestamp in **milliseconds since epoch**.
  - record.csv datetime is in UTC+8; pass the same local datetime via --fault-local
    and this script will convert it to UTC and then to milliseconds.
"""

from __future__ import annotations

import argparse
import os
from datetime import timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402


PRE_BUFFER_MIN = 30
POST_BUFFER_MIN = 30
BUCKET_MINUTES = 1

# Simple color/linestyle palette (reuse telecom style)
COLORS = [
    "#e6194b",
    "#3cb44b",
    "#4363d8",
    "#f58231",
    "#911eb4",
    "#42d4f4",
    "#f032e6",
    "#000000",
    "#ffe119",
    "#469990",
    "#9A6324",
    "#800000",
    "#aaffc3",
    "#000075",
    "#a9a9a9",
    "#808000",
    "#ffd8b1",
    "#bfef45",
    "#fabed4",
    "#dcbeff",
    "#fffac8",
    "#ff6961",
]
LINESTYLES = ["-", "--", "-.", ":"]


def load_trace_window(csv_path: Path, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Stream-read Market trace CSV, filtering to [start_ms, end_ms].

    Market trace_span.csv columns:
      timestamp (ms), cmdb_id, span_id, trace_id, duration, type, status_code, operation_name, parent_span
    """
    chunks: list[pd.DataFrame] = []
    for chunk in pd.read_csv(csv_path, chunksize=500_000):
        # timestamp is in milliseconds
        mask = (chunk["timestamp"] >= start_ms) & (chunk["timestamp"] <= end_ms)
        filtered = chunk[mask]
        if not filtered.empty:
            chunks.append(filtered)

    if not chunks:
        return pd.DataFrame()

    df = pd.concat(chunks, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["bucket"] = df["datetime"].dt.floor(f"{BUCKET_MINUTES}min")

    # status_code: 0 = success, non-zero = error
    if df["status_code"].dtype == object:
        df["error_bool"] = df["status_code"].astype(str).str.strip() != "0"
    else:
        df["error_bool"] = df["status_code"] != 0

    return df


def plot_component_latency(
    df: pd.DataFrame,
    fault_ts: pd.Timestamp,
    out_dir: Path,
    fault_component: str | None = None,
) -> None:
    """One figure: p50 latency + error rate for all cmdb_id peers."""
    if df.empty:
        print("    No trace data to plot.")
        return

    components = sorted(df["cmdb_id"].dropna().unique())
    print(f"    Components: {len(components)}, spans: {len(df)}")

    fig, (ax_lat, ax_err) = plt.subplots(2, 1, figsize=(16, 10), sharex=True)

    for ci, comp in enumerate(components):
        comp_df = df[df["cmdb_id"] == comp]
        if comp_df.empty:
            continue
        color = COLORS[ci % len(COLORS)]
        ls = LINESTYLES[ci // len(COLORS)]
        lw = 2.0 if fault_component and (fault_component in comp) else 0.8
        alpha = 1.0 if fault_component and (fault_component in comp) else 0.7

        lat = comp_df.groupby("bucket")["duration"].median()
        ax_lat.plot(
            lat.index,
            lat.values,
            label=comp,
            linewidth=lw,
            alpha=alpha,
            color=color,
            linestyle=ls,
        )

        err = comp_df.groupby("bucket")["error_bool"].apply(
            lambda s: (s.mean()) * 100 if len(s) > 0 else 0.0
        )
        ax_err.plot(
            err.index,
            err.values,
            label=comp,
            linewidth=lw,
            alpha=alpha,
            color=color,
            linestyle=ls,
        )

    for ax in (ax_lat, ax_err):
        ax.axvline(
            fault_ts,
            color="red",
            linestyle="--",
            linewidth=1.5,
            alpha=0.7,
            label="fault time",
        )

    ax_lat.set_title("Trace Latency (p50 duration) per Component", fontsize=13, fontweight="bold")
    ax_lat.set_ylabel("Duration (ms)")
    ax_lat.grid(True, alpha=0.3)
    ax_lat.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_lat.legend(loc="upper right", fontsize=7, ncol=2)

    ax_err.set_title("Trace Error Rate per Component", fontsize=13, fontweight="bold")
    ax_err.set_xlabel("Time (UTC)")
    ax_err.set_ylabel("Error Rate (%)")
    ax_err.grid(True, alpha=0.3)
    ax_err.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_err.legend(loc="upper right", fontsize=7, ncol=2)

    plt.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    fpath = out_dir / "trace_market_latency.png"
    fig.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"    Saved: {fpath}")


def plot_volume(df: pd.DataFrame, fault_ts: pd.Timestamp, out_dir: Path) -> None:
    """Bar chart of total span count per bucket."""
    if df.empty:
        return
    fig, ax = plt.subplots(figsize=(16, 5))
    vol = df.groupby("bucket").size()
    ax.bar(vol.index, vol.values, width=timedelta(seconds=50), alpha=0.7, color="steelblue")
    ax.axvline(
        fault_ts,
        color="red",
        linestyle="--",
        linewidth=1.5,
        alpha=0.7,
        label="fault time",
    )
    ax.set_title("Total Trace Volume per Minute", fontsize=13, fontweight="bold")
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Span Count")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.legend()
    plt.tight_layout()
    fpath = out_dir / "trace_market_volume.png"
    fig.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"    Saved: {fpath}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Visualize Market trace latency around a fault")
    ap.add_argument(
        "--trace-csv",
        required=True,
        help="Path to Market trace_span.csv (timestamp in ms)",
    )
    ap.add_argument(
        "--component",
        required=False,
        help="Fault component name (e.g., cartservice, frontend-1) for highlighting",
    )
    ap.add_argument(
        "--reason",
        required=False,
        help="Fault reason (for logging only)",
    )
    ap.add_argument(
        "--fault-local",
        required=True,
        help="Fault datetime in local UTC+8, e.g. '2022-03-20 21:16:21'",
    )
    ap.add_argument(
        "--pre-min",
        type=int,
        default=PRE_BUFFER_MIN,
        help="Minutes before fault time",
    )
    ap.add_argument(
        "--post-min",
        type=int,
        default=POST_BUFFER_MIN,
        help="Minutes after fault time",
    )
    ap.add_argument(
        "--output-dir",
        required=True,
        help="Output directory for PNGs",
    )
    args = ap.parse_args()

    trace_csv = Path(args.trace_csv)
    if not trace_csv.exists():
        raise FileNotFoundError(f"Trace CSV not found: {trace_csv}")

    # fault-local (UTC+8) → UTC
    local_ts = pd.to_datetime(args.fault_local).tz_localize("Asia/Shanghai")
    fault_ts_utc = local_ts.tz_convert("UTC")
    query_start = fault_ts_utc - pd.Timedelta(minutes=args.pre_min)
    query_end = fault_ts_utc + pd.Timedelta(minutes=args.post_min)

    print("=" * 70)
    print(f"  Market trace visualization")
    print(f"  Component: {args.component or '(all)'} / Reason: {args.reason or '(n/a)'}")
    print(f"  Fault (UTC): {fault_ts_utc}")
    print(f"  Query window (UTC): {query_start} ~ {query_end}")
    print(f"  Trace CSV: {trace_csv}")
    print("=" * 70)

    start_ms = int(query_start.timestamp() * 1000)
    end_ms = int(query_end.timestamp() * 1000)

    df = load_trace_window(trace_csv, start_ms, end_ms)
    if df.empty:
        print("  !! No trace data in window")
        return

    print(f"  Loaded {len(df)} spans")

    out_dir = Path(args.output_dir)
    plot_component_latency(df, fault_ts_utc, out_dir, fault_component=args.component)
    plot_volume(df, fault_ts_utc, out_dir)


if __name__ == "__main__":
    main()

