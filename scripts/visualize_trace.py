#!/usr/bin/env python3
"""Visualize trace latency & error rate per component around network fault windows.

Usage:
    python scripts/visualize_trace.py

Generates PNG for each case in OUTPUT_BASE/<case_label>/.
"""

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import os
from datetime import datetime, timezone, timedelta

# ── Cases: (label, component, reason, fault_unix_ts, telemetry_dir) ─────
# record.csv datetime is UTC+8; directory name = UTC+8 date
# query window = fault_time ± 30 min (pre_buffer/post_buffer from config)
TELECOM_BASE = "aiopslab-applications/static_dataset/openrca/Telecom/telemetry"

CASES = [
    {
        "label": "case_delay_os018_0521",
        "component": "os_018",
        "reason": "network delay",
        "fault_ts": 1590083280,   # 2020-05-21 17:48 UTC
        "trace_csv": f"{TELECOM_BASE}/2020_05_22/trace/trace_span.csv",
    },
    {
        "label": "case_loss_os021_0522a",
        "component": "os_021",
        "reason": "network loss",
        "fault_ts": 1590167760,   # 2020-05-22 17:16 UTC
        "trace_csv": f"{TELECOM_BASE}/2020_05_23/trace/trace_span.csv",
    },
    {
        "label": "case_delay_os021_0522b",
        "component": "os_021",
        "reason": "network delay",
        "fault_ts": 1590176160,   # 2020-05-22 19:36 UTC
        "trace_csv": f"{TELECOM_BASE}/2020_05_23/trace/trace_span.csv",
    },
    {
        "label": "case_loss_os017_0524",
        "component": "os_017",
        "reason": "network loss",
        "fault_ts": 1590349620,   # 2020-05-24 19:47 UTC
        "trace_csv": f"{TELECOM_BASE}/2020_05_25/trace/trace_span.csv",
    },
]

PRE_BUFFER_MIN = 30
POST_BUFFER_MIN = 30
BUCKET_MINUTES = 1
OUTPUT_BASE = "results/trace_viz"

# ── Color palette (22 distinct) ────────────────────────────────────────
COLORS = [
    '#e6194b', '#3cb44b', '#4363d8', '#f58231', '#911eb4',
    '#42d4f4', '#f032e6', '#000000', '#ffe119', '#469990',
    '#9A6324', '#800000', '#aaffc3', '#000075', '#a9a9a9',
    '#808000', '#ffd8b1', '#bfef45', '#fabed4', '#dcbeff',
    '#fffac8', '#ff6961',
]
LINESTYLES = ['-', '--', '-.', ':']


def load_trace_window(csv_path: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Stream-read trace CSV, filtering to [start_ms, end_ms]."""
    chunks = []
    for chunk in pd.read_csv(csv_path, chunksize=500_000):
        mask = (chunk["startTime"] >= start_ms) & (chunk["startTime"] <= end_ms)
        filtered = chunk[mask]
        if not filtered.empty:
            chunks.append(filtered)

    if not chunks:
        return pd.DataFrame()

    df = pd.concat(chunks, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["startTime"], unit="ms", utc=True)
    df["bucket"] = df["datetime"].dt.floor(f"{BUCKET_MINUTES}min")

    if df["success"].dtype == object:
        df["success_bool"] = df["success"].str.strip().str.lower() == "true"
    else:
        df["success_bool"] = df["success"].astype(bool)

    return df


def plot_component_type(df: pd.DataFrame, comp_col: str, comp_type: str,
                        role_label: str, fault_ts: pd.Timestamp,
                        fault_comp: str, out_dir: str):
    """Two subplots: p50 latency + error rate for all peers of comp_type."""
    sub = df[df[comp_col].fillna("").str.startswith(comp_type + "_")]
    if sub.empty:
        print(f"    No data for {comp_type} ({role_label})")
        return

    components = sorted(sub[comp_col].unique())
    print(f"    {comp_type} ({role_label}): {len(components)} components, {len(sub)} spans")

    fig, (ax_lat, ax_err) = plt.subplots(2, 1, figsize=(16, 10), sharex=True)

    for ci, comp in enumerate(components):
        comp_df = sub[sub[comp_col] == comp]
        color = COLORS[ci % len(COLORS)]
        ls = LINESTYLES[ci // len(COLORS)]
        # Highlight the faulty component
        lw = 2.0 if comp == fault_comp else 0.8
        alpha = 1.0 if comp == fault_comp else 0.7

        lat = comp_df.groupby("bucket")["elapsedTime"].median()
        ax_lat.plot(lat.index, lat.values, label=comp,
                    linewidth=lw, alpha=alpha, color=color, linestyle=ls)

        err = comp_df.groupby("bucket")["success_bool"].apply(
            lambda s: (1 - s.mean()) * 100
        )
        ax_err.plot(err.index, err.values, label=comp,
                    linewidth=lw, alpha=alpha, color=color, linestyle=ls)

    for ax in (ax_lat, ax_err):
        ax.axvline(fault_ts, color='red', linestyle='--', linewidth=1.5,
                   alpha=0.7, label='fault time')

    ax_lat.set_title(f"{comp_type} {role_label} — Trace Latency (p50)",
                     fontsize=13, fontweight="bold")
    ax_lat.set_ylabel("Elapsed Time (ms)")
    ax_lat.grid(True, alpha=0.3)
    ax_lat.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_lat.legend(loc="upper right", fontsize=7, ncol=2)

    ax_err.set_title(f"{comp_type} {role_label} — Trace Error Rate",
                     fontsize=13, fontweight="bold")
    ax_err.set_xlabel("Time (UTC)")
    ax_err.set_ylabel("Error Rate (%)")
    ax_err.grid(True, alpha=0.3)
    ax_err.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_err.legend(loc="upper right", fontsize=7, ncol=2)

    plt.tight_layout()
    fname = f"trace_{comp_type}_{role_label}.png"
    fpath = os.path.join(out_dir, fname)
    fig.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"    Saved: {fpath}")


def plot_volume(df: pd.DataFrame, fault_ts: pd.Timestamp, out_dir: str):
    """Bar chart of total span count per bucket."""
    fig, ax = plt.subplots(figsize=(16, 5))
    vol = df.groupby("bucket").size()
    ax.bar(vol.index, vol.values, width=pd.Timedelta(seconds=50),
           alpha=0.7, color='steelblue')
    ax.axvline(fault_ts, color='red', linestyle='--', linewidth=1.5,
               alpha=0.7, label='fault time')
    ax.set_title("Total Trace Volume per Minute", fontsize=13, fontweight="bold")
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Span Count")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.legend()
    plt.tight_layout()
    fpath = os.path.join(out_dir, "trace_volume.png")
    fig.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"    Saved: {fpath}")


def process_case(case: dict):
    fault_ts_utc = pd.Timestamp(case["fault_ts"], unit="s", tz="UTC")
    query_start = fault_ts_utc - pd.Timedelta(minutes=PRE_BUFFER_MIN)
    query_end = fault_ts_utc + pd.Timedelta(minutes=POST_BUFFER_MIN)

    label = case["label"]
    comp = case["component"]
    reason = case["reason"]

    print(f"\n{'='*70}")
    print(f"  {label}: {comp} / {reason}")
    print(f"  Fault:  {fault_ts_utc}")
    print(f"  Query:  {query_start} ~ {query_end}")
    print(f"  Trace:  {case['trace_csv']}")
    print(f"{'='*70}")

    start_ms = int(query_start.timestamp() * 1000)
    end_ms = int(query_end.timestamp() * 1000)

    df = load_trace_window(case["trace_csv"], start_ms, end_ms)
    if df.empty:
        print("  !! No trace data in window")
        return

    print(f"  Loaded {len(df)} spans")

    out_dir = os.path.join(OUTPUT_BASE, label)
    os.makedirs(out_dir, exist_ok=True)

    # Identify component types present
    caller_types = sorted(set(
        c.rsplit("_", 1)[0] for c in df["cmdb_id"].dropna().unique()
    ))
    callee_types = sorted(set(
        c.rsplit("_", 1)[0] for c in df["dsName"].dropna().unique()
    ))
    print(f"  Caller types: {caller_types}")
    print(f"  Callee types: {callee_types}")

    for ct in caller_types:
        plot_component_type(df, "cmdb_id", ct, "caller",
                            fault_ts_utc, comp, out_dir)

    for ct in callee_types:
        plot_component_type(df, "dsName", ct, "callee",
                            fault_ts_utc, comp, out_dir)

    plot_volume(df, fault_ts_utc, out_dir)


def main():
    for case in CASES:
        process_case(case)
    print("\nDone!")


if __name__ == "__main__":
    main()
