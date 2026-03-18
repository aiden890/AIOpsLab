#!/usr/bin/env python3
"""Visualize Market trace latency & error rate for a few predefined fault cases.

Telecom용 `visualize_trace.py`와 동일한 스타일로,
Market static-dataset에 대해 미리 고른 fault window들을 반복 시각화한다.

Usage:
    python scripts/visualize_trace_market_cases.py

결과는 OUTPUT_BASE/<label>/ 아래 PNG 두 장으로 저장된다:
  - trace_market_latency.png  (cmdb_id별 p50 duration)
  - trace_market_volume.png   (전체 span volume)
"""

from __future__ import annotations

import os
from datetime import timedelta

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402


# ── Cases: (label, component, reason, fault_unix_ts, telemetry trace_csv) ─────
# record.csv datetime는 UTC+8 기준, timestamp 컬럼은 epoch seconds.
# Market trace_span.csv는 ms 단위 timestamp를 사용한다.

MARKET_BASE = "aiopslab-applications/static_dataset/openrca/Market/cloudbed-1/telemetry"

CASES = [
    {
        "label": "market_cartservice_latency_0320",
        "component": "cartservice",
        "reason": "container network latency",
        "fault_ts": 1647736751,  # 2022-03-20 08:39:11 (record.csv)
        "trace_csv": f"{MARKET_BASE}/2022_03_20/trace/trace_span.csv",
    },
    {
        "label": "market_adservice_latency_0320",
        "component": "adservice",
        "reason": "container network latency",
        "fault_ts": 1647737329,  # 2022-03-20 08:48:49
        "trace_csv": f"{MARKET_BASE}/2022_03_20/trace/trace_span.csv",
    },
    {
        "label": "market_frontend2_pktcorr_0321",
        "component": "frontend2-0",
        "reason": "container network packet corruption",
        "fault_ts": 1647846519,  # 2022-03-21 15:08:39
        "trace_csv": f"{MARKET_BASE}/2022_03_21/trace/trace_span.csv",
    },
    {
        "label": "market_emailservice_pktloss_0320",
        "component": "emailservice",
        "reason": "container packet loss",
        "fault_ts": 1647782181,  # 2022-03-20 21:16:21
        "trace_csv": f"{MARKET_BASE}/2022_03_20/trace/trace_span.csv",
    },
]

PRE_BUFFER_MIN = 30
POST_BUFFER_MIN = 30
BUCKET_MINUTES = 1
OUTPUT_BASE = "results/trace_viz_market_cases"


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


def load_trace_window(csv_path: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Market trace_span.csv에서 [start_ms, end_ms] 윈도우를 필터링."""
    chunks = []
    for chunk in pd.read_csv(csv_path, chunksize=500_000):
        mask = (chunk["timestamp"] >= start_ms) & (chunk["timestamp"] <= end_ms)
        filtered = chunk[mask]
        if not filtered.empty:
            chunks.append(filtered)

    if not chunks:
        return pd.DataFrame()

    df = pd.concat(chunks, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df["bucket"] = df["datetime"].dt.floor(f"{BUCKET_MINUTES}min")

    if "status_code" in df.columns:
        if df["status_code"].dtype == object:
            df["error_bool"] = df["status_code"].astype(str).str.strip() != "0"
        else:
            df["error_bool"] = df["status_code"] != 0
    else:
        df["error_bool"] = False

    return df


def plot_component_latency(
    df: pd.DataFrame,
    fault_ts: pd.Timestamp,
    out_dir: str,
    fault_comp: str,
) -> None:
    """두 서브플롯: cmdb_id별 p50 duration + error rate."""
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
        lw = 2.0 if fault_comp in comp else 0.8
        alpha = 1.0 if fault_comp in comp else 0.7

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

    ax_lat.set_title("Market Trace Latency (p50 duration) per Component", fontsize=13, fontweight="bold")
    ax_lat.set_ylabel("Duration (ms)")
    ax_lat.grid(True, alpha=0.3)
    ax_lat.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_lat.legend(loc="upper right", fontsize=7, ncol=2)

    ax_err.set_title("Market Trace Error Rate per Component", fontsize=13, fontweight="bold")
    ax_err.set_xlabel("Time (UTC)")
    ax_err.set_ylabel("Error Rate (%)")
    ax_err.grid(True, alpha=0.3)
    ax_err.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_err.legend(loc="upper right", fontsize=7, ncol=2)

    plt.tight_layout()
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, "trace_market_latency.png")
    fig.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"    Saved: {fpath}")


def plot_volume(df: pd.DataFrame, fault_ts: pd.Timestamp, out_dir: str) -> None:
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
    ax.set_title("Total Market Trace Volume per Minute", fontsize=13, fontweight="bold")
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Span Count")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.legend()
    plt.tight_layout()
    os.makedirs(out_dir, exist_ok=True)
    fpath = os.path.join(out_dir, "trace_market_volume.png")
    fig.savefig(fpath, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"    Saved: {fpath}")


def process_case(case: dict) -> None:
    fault_ts_utc = pd.Timestamp(case["fault_ts"], unit="s", tz="UTC")
    query_start = fault_ts_utc - pd.Timedelta(minutes=PRE_BUFFER_MIN)
    query_end = fault_ts_utc + pd.Timedelta(minutes=POST_BUFFER_MIN)

    label = case["label"]
    comp = case["component"]
    reason = case["reason"]

    print("\n" + "=" * 70)
    print(f"  {label}: {comp} / {reason}")
    print(f"  Fault (UTC):  {fault_ts_utc}")
    print(f"  Query window: {query_start} ~ {query_end}")
    print(f"  Trace CSV:    {case['trace_csv']}")
    print("=" * 70)

    start_ms = int(query_start.timestamp() * 1000)
    end_ms = int(query_end.timestamp() * 1000)

    df = load_trace_window(case["trace_csv"], start_ms, end_ms)
    if df.empty:
        print("  !! No trace data in window")
        return

    print(f"  Loaded {len(df)} spans")

    out_dir = os.path.join(OUTPUT_BASE, label)
    os.makedirs(out_dir, exist_ok=True)

    plot_component_latency(df, fault_ts_utc, out_dir, fault_comp=comp)
    plot_volume(df, fault_ts_utc, out_dir)


def main() -> None:
    for case in CASES:
        process_case(case)
    print("\nDone!")


if __name__ == "__main__":
    main()

