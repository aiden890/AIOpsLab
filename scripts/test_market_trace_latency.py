"""Ad-hoc script to visualize trace latency around known network faults (Market dataset).

Usage:
    python scripts/test_market_trace_latency.py \\
        --dataset-root aiopslab-applications/static_dataset/openrca/Market/cloudbed-1 \\
        --max-faults 3

This script:
  1. Reads record.csv (UTC+8 timestamps) and picks a few network-related faults.
  2. For each fault, converts the fault time from UTC+8 → UTC (−8h).
  3. Loads trace_span.csv and extracts a ±15 minute window around the (UTC) fault time.
  4. Computes per-minute p50 latency and error rate per cmdb_id.
  5. Plots latency time-series, highlighting the fault component against its peers.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import List

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402


NETWORK_REASONS = {
    "container network latency",
    "container network packet retransmission",
    "container network packet corruption",
    "container packet loss",
}


def _load_record(df_path: Path) -> pd.DataFrame:
    df = pd.read_csv(df_path)
    # Normalize column names just in case
    df.columns = [c.strip() for c in df.columns]
    return df


def _filter_network_faults(df: pd.DataFrame, max_faults: int) -> pd.DataFrame:
    df_net = df[df["reason"].isin(NETWORK_REASONS)].copy()
    df_net = df_net.sort_values("timestamp").head(max_faults)
    return df_net


def _fault_time_utc(row: pd.Series) -> pd.Timestamp:
    """record.csv stores datetime in UTC+8 (Asia/Shanghai). Convert to naive UTC."""
    dt_str = str(row["datetime"])
    # Parse as UTC+8 and shift to UTC
    dt_local = pd.to_datetime(dt_str).tz_localize("Asia/Shanghai")
    dt_utc = dt_local.tz_convert("UTC")
    # Return tz-naive UTC for easier comparison with trace timestamps
    return dt_utc.tz_localize(None)


def _load_traces(trace_root: Path) -> pd.DataFrame:
    """Load trace spans from static_dataset trace directory.

    Market static dataset stores traces under:
      telemetry/YYYY_MM_DD/trace/*.csv
    We merge all CSVs under that trace directory.
    """
    if trace_root.is_file():
        paths = [trace_root]
    else:
        if trace_root.is_dir():
            paths = sorted(trace_root.rglob("*.csv"))
        else:
            raise FileNotFoundError(f"Trace root not found: {trace_root}")
    if not paths:
        raise FileNotFoundError(f"No trace CSV files found under {trace_root}")

    dfs = []
    for p in paths:
        try:
            dfs.append(pd.read_csv(p))
        except Exception:
            continue
    if not dfs:
        raise ValueError(f"Failed to read any trace CSVs under {trace_root}")
    df = pd.concat(dfs, ignore_index=True)
    # Expected columns from workflow_market.py:
    # timestamp, cmdb_id, span_id, trace_id, duration, type, status_code, operation_name, parent_span
    df.columns = [c.strip() for c in df.columns]
    if "timestamp" not in df.columns:
        raise ValueError("trace_span.csv missing 'timestamp' column")
    # timestamp is Unix seconds in dataset; convert to pandas datetime (UTC)
    df["ts_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_localize(None)
    return df


def _plot_latency_for_fault(
    trace_df: pd.DataFrame,
    fault_row: pd.Series,
    out_dir: Path,
    window_minutes: int = 15,
) -> None:
    comp = str(fault_row["component"])
    reason = str(fault_row["reason"])
    fault_utc = _fault_time_utc(fault_row)

    t_start = fault_utc - pd.Timedelta(minutes=window_minutes)
    t_end = fault_utc + pd.Timedelta(minutes=window_minutes)

    window_df = trace_df[(trace_df["ts_utc"] >= t_start) & (trace_df["ts_utc"] <= t_end)].copy()
    if window_df.empty:
        print(f"[WARN] No trace data in window for fault {comp} @ {fault_utc}")
        return

    # Derive latency_ms from duration if needed
    if "duration" in window_df.columns and "latency_ms" not in window_df.columns:
        # duration may already be in ms or us; assume ms if not specified
        window_df["latency_ms"] = window_df["duration"].astype(float)
    elif "latency_ms" not in window_df.columns:
        raise ValueError("trace_span.csv missing 'duration' or 'latency_ms' columns")

    # status_code: 0 = success, non-zero = error (per workflow_market.py)
    if "status_code" not in window_df.columns:
        window_df["status_code"] = 0

    # Resample to 1-min buckets per component
    window_df["minute"] = window_df["ts_utc"].dt.floor("1min")
    grp = window_df.groupby(["cmdb_id", "minute"])
    agg = grp.agg(
        p50_latency_ms=("latency_ms", lambda x: x.quantile(0.5)),
        error_rate=("status_code", lambda x: (x != 0).mean() if len(x) > 0 else 0.0),
        count=("span_id", "count") if "span_id" in window_df.columns else ("timestamp", "count"),
    ).reset_index()

    # Plot latency for all components; highlight the fault component (service/pod)
    out_dir.mkdir(parents=True, exist_ok=True)
    fig, ax1 = plt.subplots(figsize=(10, 4))

    cmdb_ids: List[str] = sorted(agg["cmdb_id"].unique())
    for cid in cmdb_ids:
        sub = agg[agg["cmdb_id"] == cid]
        if sub.empty:
            continue
        style = "-" if cid == comp else "--"
        alpha = 1.0 if cid == comp else 0.3
        label = f"{cid} (fault)" if cid == comp else cid
        ax1.plot(
            sub["minute"],
            sub["p50_latency_ms"],
            style,
            alpha=alpha,
            label=label,
        )

    ax1.axvline(fault_utc, color="red", linestyle=":", label="fault time")
    ax1.set_title(f"Trace latency around fault: {comp} — {reason}")
    ax1.set_xlabel("Time (UTC)")
    ax1.set_ylabel("p50 latency (ms)")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    ax1.legend(loc="best", fontsize=8, ncol=2)
    ax1.grid(True, alpha=0.3)

    out_path = out_dir / f"trace_latency_{comp}_{fault_utc.strftime('%Y%m%d_%H%M%S')}.png"
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    print(f"[INFO] Saved trace latency plot: {out_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Market trace latency test script")
    parser.add_argument(
        "--dataset-root",
        type=str,
        required=True,
        help="Path to Market dataset root (directory containing record.csv and telemetry/...)",
    )
    parser.add_argument(
        "--max-faults",
        type=int,
        default=3,
        help="Maximum number of network faults to visualize",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=15,
        help="Minutes before/after fault time for the trace window",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="market_trace_latency_plots",
        help="Directory to save generated PNGs",
    )
    args = parser.parse_args()

    dataset_root = Path(args.dataset_root)
    record_path = dataset_root / "record.csv"
    # For Market static-dataset, traces live under telemetry/YYYY_MM_DD/trace/*.csv
    trace_root = dataset_root / "telemetry" / "2022_03_20" / "trace"

    if not record_path.exists():
        raise FileNotFoundError(f"record.csv not found at {record_path}")

    print(f"[INFO] Loading record.csv from {record_path}")
    rec_df = _load_record(record_path)
    net_df = _filter_network_faults(rec_df, args.max_faults)
    if net_df.empty:
        print("[WARN] No network-related faults found in record.csv")
        return

    print(f"[INFO] Selecting {len(net_df)} network faults for trace latency plots")
    trace_df = _load_traces(trace_root)
    out_dir = Path(args.output_dir)

    for _, row in net_df.iterrows():
        _plot_latency_for_fault(
            trace_df=trace_df,
            fault_row=row,
            out_dir=out_dir,
            window_minutes=args.window_minutes,
        )


if __name__ == "__main__":
    # Allow running via `python -m` or direct call.
    # Ensure matplotlib does not try to use an interactive backend.
    os.environ.setdefault("MPLBACKEND", "Agg")
    main()

