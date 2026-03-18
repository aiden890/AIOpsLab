"""Visualize 30-min metric windows for OpenRCA Telecom queries.

Plots per-component-type peer comparison charts for key KPIs,
highlighting the root cause component and fault time.

Usage:
    python scripts/visualize_telecom_metrics.py
"""

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

# ── Configuration ──────────────────────────────────────────────────────────

DATA_ROOT = Path("aiopslab-applications/static_dataset/openrca/Telecom")
OUT_DIR = Path("scripts/telecom_metric_plots")

# ── Display Options ────────────────────────────────────────────────────────
HIGHLIGHT_ROOT_CAUSE = False   # True: root cause를 빨간 굵은 선으로 강조
SHOW_FAULT_TIME = False        # True: fault 발생 시각에 빨간 점선 표시

# record.csv의 datetime은 UTC+8 → UTC로 변환하려면 -8h
# 실제 metric CSV의 timestamp는 UTC 기준

# 5 diverse queries (시간은 모두 UTC 기준)
# record.csv datetime에서 -8h 적용
QUERIES = [
    {
        "id": "task_2-0",
        "date_dir": "2020_04_11",
        "start": "2020-04-10 16:00:00",   # record: 04-11 00:00 UTC+8 → -8h
        "end":   "2020-04-10 16:30:00",   # record: 04-11 00:30 UTC+8 → -8h
        "fault_time": "2020-04-10 16:05:00",  # record: 04-11 00:05 UTC+8 → -8h
        "root_cause": "docker_003",
        "reason": "CPU fault",
    },
    {
        "id": "task_2-2",
        "date_dir": "2020_04_11",
        "start": "2020-04-10 18:00:00",   # record: 04-11 02:00 UTC+8
        "end":   "2020-04-10 18:30:00",
        "fault_time": "2020-04-10 18:15:00",
        "root_cause": "db_007",
        "reason": "db connection limit",
    },
    {
        "id": "task_7-4",
        "date_dir": "2020_04_11",
        "start": "2020-04-10 20:30:00",   # record: 04-11 04:30 UTC+8
        "end":   "2020-04-10 21:00:00",
        "fault_time": "2020-04-10 20:40:00",
        "root_cause": "docker_008",
        "reason": "CPU fault",
    },
    {
        "id": "task_4-8",
        "date_dir": "2020_05_22",
        "start": "2020-05-21 17:30:00",   # record: 05-22 01:30 UTC+8
        "end":   "2020-05-21 18:00:00",
        "fault_time": "2020-05-21 17:48:00",
        "root_cause": "os_018",
        "reason": "network delay",
    },
    {
        "id": "task_6-12",
        "date_dir": "2020_05_23",
        "start": "2020-05-22 17:00:00",   # record: 05-23 01:00 UTC+8
        "end":   "2020-05-22 17:30:00",
        "fault_time": "2020-05-22 17:16:00",
        "root_cause": "os_021",
        "reason": "network loss",
    },
]

# Key KPIs: (component_type, reason) → KPI list
# reason이 None이면 해당 component_type의 기본 KPI
KEY_KPIS_BY_REASON = {
    # ── docker ──
    ("docker", "CPU fault"): [
        "container_cpu_used",
        "container_mem_used",
        "container_thread_used_pct",
    ],
    ("docker", None): [
        "container_cpu_used",
        "container_mem_used",
        "container_thread_used_pct",
    ],
    # ── os: network ──
    ("os", "network delay"): [
        "Received_queue",
        "Sent_queue",
        "System_wait_queue_length",
        "ICMP_ping",
        "Agent_ping",
        "Processor_load_1_min",
    ],
    ("os", "network loss"): [
        "Received_errors_packets",
        "Sent_errors_packets",
        "Received_packets",
        "Sent_packets",
        "Outgoing_network_traffic",
        "Incoming_network_traffic",
    ],
    ("os", None): [
        "CPU_util_pct",
        "Memory_used_pct",
        "Disk_io_util",
        "Outgoing_network_traffic",
    ],
    # ── db: connection ──
    ("db", "db connection limit"): [
        "Sess_Connect",
        "Sess_Active",
        "Proc_Used_Pct",
        "Login_Per_Sec",
        "Session_pct",
    ],
    ("db", "db close"): [
        "Sess_Connect",
        "Sess_Active",
        "On_Off_State",
        "Login_Per_Sec",
        "TPS_Per_Sec",
    ],
    ("db", None): [
        "CPU_Used_Pct",
        "Sess_Active",
        "Physical_Read_Per_Sec",
        "Redo_Per_Sec",
    ],
    # ── redis ──
    ("redis", None): [
        "connected_clients",
        "used_memory",
    ],
}


def get_kpis(ctype: str, reason: str) -> list[str]:
    """Get KPI list for component type + reason, fallback to default."""
    kpis = KEY_KPIS_BY_REASON.get((ctype, reason))
    if kpis is None:
        kpis = KEY_KPIS_BY_REASON.get((ctype, None), [])
    return kpis

# CSV → component type mapping
CSV_MAP = {
    "metric_container.csv": "docker",
    "metric_node.csv": "os",
    "metric_service.csv": "db",
    "metric_middleware.csv": "redis",
}


# ── Data Loading ───────────────────────────────────────────────────────────

def load_metrics(date_dir: str) -> pd.DataFrame:
    """Load all metric CSVs for a given date, return combined DataFrame."""
    metric_dir = DATA_ROOT / "telemetry" / date_dir / "metric"
    frames = []

    for csv_name, ctype in CSV_MAP.items():
        csv_path = metric_dir / csv_name
        if not csv_path.exists():
            continue
        df = pd.read_csv(csv_path)

        # Normalize timestamp column
        if "timestamp" in df.columns:
            ts_col = "timestamp"
        elif "startTime" in df.columns:
            ts_col = "startTime"
        else:
            continue

        # Normalize KPI name column
        if "name" in df.columns:
            kpi_col = "name"
        else:
            continue

        df = df.rename(columns={ts_col: "timestamp", kpi_col: "kpi_name"})
        df["component_type"] = ctype

        cols = ["timestamp", "kpi_name", "value", "cmdb_id", "component_type"]
        cols = [c for c in cols if c in df.columns]
        frames.append(df[cols])

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.dropna(subset=["timestamp", "value", "cmdb_id"])

    # Convert ms → seconds if needed
    med = combined["timestamp"].median()
    if med > 1e12:
        combined["timestamp"] = combined["timestamp"] / 1000.0

    # Convert to UTC datetime
    combined["datetime"] = pd.to_datetime(combined["timestamp"], unit="s", utc=True)

    return combined


def filter_window(df: pd.DataFrame, start_str: str, end_str: str) -> pd.DataFrame:
    """Filter DataFrame to [start, end] UTC time window."""
    start = pd.Timestamp(start_str, tz="UTC")
    end = pd.Timestamp(end_str, tz="UTC")
    return df[(df["datetime"] >= start) & (df["datetime"] <= end)]


# ── Plotting ───────────────────────────────────────────────────────────────

def plot_query(query: dict, all_metrics: pd.DataFrame):
    """Plot peer comparison charts for one query."""
    qid = query["id"]
    root_cause = query["root_cause"]
    reason = query["reason"]
    fault_time = pd.Timestamp(query["fault_time"], tz="UTC")

    # Root cause component type
    rc_type = root_cause.split("_")[0]

    # Plot root cause type + related type
    types_to_plot = [rc_type]
    if rc_type == "docker":
        types_to_plot.append("os")
    elif rc_type == "os":
        types_to_plot.append("docker")

    kpis_plotted = 0
    for ctype in types_to_plot:
        kpis = get_kpis(ctype, reason)
        type_df = all_metrics[all_metrics["component_type"] == ctype]
        if type_df.empty:
            continue

        available_kpis = type_df["kpi_name"].unique()
        kpis = [k for k in kpis if k in available_kpis]
        if not kpis:
            continue

        components = sorted(type_df["cmdb_id"].unique())

        n_kpis = len(kpis)
        fig, axes = plt.subplots(n_kpis, 1, figsize=(14, 4 * n_kpis), sharex=True)
        if n_kpis == 1:
            axes = [axes]

        fig.suptitle(
            f"{qid}: {reason} @ {root_cause}\n"
            f"Window: {query['start']} ~ {query['end']} (UTC)",
            fontsize=14, fontweight="bold", y=1.02,
        )

        for ax, kpi in zip(axes, kpis):
            kpi_df = type_df[type_df["kpi_name"] == kpi]

            for comp in components:
                comp_df = kpi_df[kpi_df["cmdb_id"] == comp].sort_values("datetime")
                if comp_df.empty:
                    continue

                is_rc = (comp == root_cause)
                if HIGHLIGHT_ROOT_CAUSE and is_rc:
                    ax.plot(
                        comp_df["datetime"], comp_df["value"],
                        label=comp, linewidth=3.0, alpha=1.0,
                        color="red", zorder=10,
                    )
                else:
                    ax.plot(
                        comp_df["datetime"], comp_df["value"],
                        label=comp, linewidth=0.8, alpha=0.4,
                        zorder=1,
                    )

            # Fault time marker
            if SHOW_FAULT_TIME:
                ax.axvline(
                    fault_time, color="red", linestyle="--",
                    linewidth=1.5, alpha=0.7, label="fault time",
                )

            ax.set_ylabel(kpi, fontsize=10)
            ax.grid(True, alpha=0.3)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

            # Legend: show all components individually
            ax.legend(loc="upper right", fontsize=7, ncol=2)

        axes[-1].set_xlabel("Time (UTC)", fontsize=10)
        plt.tight_layout()

        out_path = OUT_DIR / f"{qid}_{ctype}_metrics.png"
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
        print(f"  Saved: {out_path}")
        kpis_plotted += 1

    return kpis_plotted


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    metrics_cache: dict[str, pd.DataFrame] = {}

    for query in QUERIES:
        print(f"\n{'='*60}")
        print(f"Processing {query['id']}: {query['reason']} @ {query['root_cause']}")
        print(f"  Window: {query['start']} ~ {query['end']} (UTC)")
        print(f"{'='*60}")

        date_dir = query["date_dir"]
        if date_dir not in metrics_cache:
            print(f"  Loading metrics for {date_dir}...")
            metrics_cache[date_dir] = load_metrics(date_dir)

        all_metrics = metrics_cache[date_dir]
        if all_metrics.empty:
            print(f"  WARNING: No metric data found for {date_dir}")
            continue

        window_df = filter_window(all_metrics, query["start"], query["end"])
        print(f"  Data points in window: {len(window_df)}")

        if window_df.empty:
            print(f"  WARNING: No data in query window!")
            continue

        n = plot_query(query, window_df)
        print(f"  Generated {n} chart(s)")

    print(f"\nAll plots saved to: {OUT_DIR}/")


if __name__ == "__main__":
    main()
