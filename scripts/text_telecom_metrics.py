"""Text-based 1-min interval metric statistics for OpenRCA Telecom queries.

For each query, computes per-component peer comparison stats (p95, p90, p5, Mean, z-score)
at 1-minute intervals and writes them to a log file.

Usage:
    python scripts/text_telecom_metrics.py
"""

import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── Configuration ──────────────────────────────────────────────────────────

DATA_ROOT = Path("aiopslab-applications/static_dataset/openrca/Telecom")
OUT_DIR = Path("scripts/telecom_metric_logs")

# ── Queries (same as visualize_telecom_metrics.py) ─────────────────────────

QUERIES = [
    {
        "id": "task_2-0",
        "date_dir": "2020_04_11",
        "start": "2020-04-10 16:00:00",
        "end":   "2020-04-10 16:30:00",
        "fault_time": "2020-04-10 16:05:00",
        "root_cause": "docker_003",
        "reason": "CPU fault",
    },
    {
        "id": "task_2-2",
        "date_dir": "2020_04_11",
        "start": "2020-04-10 18:00:00",
        "end":   "2020-04-10 18:30:00",
        "fault_time": "2020-04-10 18:15:00",
        "root_cause": "db_007",
        "reason": "db connection limit",
    },
    {
        "id": "task_7-4",
        "date_dir": "2020_04_11",
        "start": "2020-04-10 20:30:00",
        "end":   "2020-04-10 21:00:00",
        "fault_time": "2020-04-10 20:40:00",
        "root_cause": "docker_008",
        "reason": "CPU fault",
    },
    {
        "id": "task_4-8",
        "date_dir": "2020_05_22",
        "start": "2020-05-21 17:30:00",
        "end":   "2020-05-21 18:00:00",
        "fault_time": "2020-05-21 17:48:00",
        "root_cause": "os_018",
        "reason": "network delay",
    },
    {
        "id": "task_6-12",
        "date_dir": "2020_05_23",
        "start": "2020-05-22 17:00:00",
        "end":   "2020-05-22 17:30:00",
        "fault_time": "2020-05-22 17:16:00",
        "root_cause": "os_021",
        "reason": "network loss",
    },
]

# Key KPIs
KEY_KPIS_BY_REASON = {
    ("docker", "CPU fault"): [
        "container_cpu_used", "container_mem_used", "container_thread_used_pct",
    ],
    ("docker", None): [
        "container_cpu_used", "container_mem_used", "container_thread_used_pct",
    ],
    ("os", "network delay"): [
        "Received_queue", "Sent_queue", "System_wait_queue_length",
        "ICMP_ping", "Agent_ping", "Processor_load_1_min",
    ],
    ("os", "network loss"): [
        "Received_errors_packets", "Sent_errors_packets",
        "Received_packets", "Sent_packets",
        "Outgoing_network_traffic", "Incoming_network_traffic",
    ],
    ("os", None): [
        "CPU_util_pct", "Memory_used_pct", "Disk_io_util", "Outgoing_network_traffic",
    ],
    ("db", "db connection limit"): [
        "Sess_Connect", "Sess_Active", "Proc_Used_Pct", "Login_Per_Sec", "Session_pct",
    ],
    ("db", "db close"): [
        "Sess_Connect", "Sess_Active", "On_Off_State", "Login_Per_Sec", "TPS_Per_Sec",
    ],
    ("db", None): [
        "CPU_Used_Pct", "Sess_Active", "Physical_Read_Per_Sec", "Redo_Per_Sec",
    ],
    ("redis", None): ["connected_clients", "used_memory"],
}

CSV_MAP = {
    "metric_container.csv": "docker",
    "metric_node.csv": "os",
    "metric_service.csv": "db",
    "metric_middleware.csv": "redis",
}


def get_kpis(ctype: str, reason: str) -> list[str]:
    kpis = KEY_KPIS_BY_REASON.get((ctype, reason))
    if kpis is None:
        kpis = KEY_KPIS_BY_REASON.get((ctype, None), [])
    return kpis


def load_metrics(date_dir: str) -> pd.DataFrame:
    metric_dir = DATA_ROOT / "telemetry" / date_dir / "metric"
    frames = []
    for csv_name, ctype in CSV_MAP.items():
        csv_path = metric_dir / csv_name
        if not csv_path.exists():
            continue
        df = pd.read_csv(csv_path)
        if "timestamp" in df.columns:
            ts_col = "timestamp"
        elif "startTime" in df.columns:
            ts_col = "startTime"
        else:
            continue
        if "name" not in df.columns:
            continue
        df = df.rename(columns={ts_col: "timestamp", "name": "kpi_name"})
        df["component_type"] = ctype
        cols = [c for c in ["timestamp", "kpi_name", "value", "cmdb_id", "component_type"] if c in df.columns]
        frames.append(df[cols])
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.dropna(subset=["timestamp", "value", "cmdb_id"])
    if combined["timestamp"].median() > 1e12:
        combined["timestamp"] = combined["timestamp"] / 1000.0
    combined["datetime"] = pd.to_datetime(combined["timestamp"], unit="s", utc=True)
    return combined


def filter_window(df: pd.DataFrame, start_str: str, end_str: str) -> pd.DataFrame:
    start = pd.Timestamp(start_str, tz="UTC")
    end = pd.Timestamp(end_str, tz="UTC")
    return df[(df["datetime"] >= start) & (df["datetime"] <= end)]


def compute_zscore(value: float, mean: float, std: float) -> float:
    if std == 0 or np.isnan(std):
        return 0.0
    return (value - mean) / std


def write_query_log(query: dict, window_df: pd.DataFrame, log_file) -> int:
    """Write text stats for one query. Returns number of KPI sections written."""
    qid = query["id"]
    root_cause = query["root_cause"]
    reason = query["reason"]
    fault_time_str = query["fault_time"]

    rc_type = root_cause.split("_")[0]
    types_to_plot = [rc_type]
    if rc_type == "docker":
        types_to_plot.append("os")
    elif rc_type == "os":
        types_to_plot.append("docker")

    sections = 0

    for ctype in types_to_plot:
        kpis = get_kpis(ctype, reason)
        type_df = window_df[window_df["component_type"] == ctype]
        if type_df.empty:
            continue

        available_kpis = type_df["kpi_name"].unique()
        kpis = [k for k in kpis if k in available_kpis]
        if not kpis:
            continue

        components = sorted(type_df["cmdb_id"].unique())

        for kpi in kpis:
            kpi_df = type_df[type_df["kpi_name"] == kpi].copy()
            if kpi_df.empty:
                continue

            # 1-minute bins
            kpi_df["minute"] = kpi_df["datetime"].dt.floor("1min")
            minutes = sorted(kpi_df["minute"].unique())

            header = f"\n{'='*100}\n"
            header += f"[{qid}] Component Type: {ctype} | KPI: {kpi}\n"
            header += f"Root Cause: {root_cause} | Reason: {reason} | Fault Time: {fault_time_str}\n"
            header += f"Peers: {', '.join(components)}\n"
            header += f"{'='*100}\n"
            log_file.write(header)

            # Table header
            col_headers = ["Time(UTC)"] + components + ["Peer_Mean", "Peer_Std", "P5", "P90", "P95"]
            # Add z-score columns for each component
            zscore_headers = [f"z({c})" for c in components]
            col_headers += zscore_headers

            # Calculate column widths
            col_width = 14
            table_header = " | ".join(h.rjust(col_width) for h in col_headers)
            separator = "-" * len(table_header)

            log_file.write(f"{table_header}\n")
            log_file.write(f"{separator}\n")

            total_lines = 0
            total_chars = 0

            for minute in minutes:
                min_df = kpi_df[kpi_df["minute"] == minute]
                time_str = minute.strftime("%H:%M")

                # Per-component mean in this minute
                comp_values = {}
                for comp in components:
                    comp_min = min_df[min_df["cmdb_id"] == comp]["value"]
                    if len(comp_min) > 0:
                        comp_values[comp] = comp_min.mean()
                    else:
                        comp_values[comp] = np.nan

                all_vals = [v for v in comp_values.values() if not np.isnan(v)]
                if not all_vals:
                    continue

                peer_mean = np.mean(all_vals)
                peer_std = np.std(all_vals)
                p5 = np.percentile(all_vals, 5)
                p90 = np.percentile(all_vals, 90)
                p95 = np.percentile(all_vals, 95)

                # Z-scores
                zscores = {}
                for comp in components:
                    v = comp_values[comp]
                    if np.isnan(v):
                        zscores[comp] = np.nan
                    else:
                        zscores[comp] = compute_zscore(v, peer_mean, peer_std)

                # Build row
                row_parts = [time_str.rjust(col_width)]
                for comp in components:
                    v = comp_values[comp]
                    row_parts.append(("N/A" if np.isnan(v) else f"{v:.4f}").rjust(col_width))
                row_parts.append(f"{peer_mean:.4f}".rjust(col_width))
                row_parts.append(f"{peer_std:.4f}".rjust(col_width))
                row_parts.append(f"{p5:.4f}".rjust(col_width))
                row_parts.append(f"{p90:.4f}".rjust(col_width))
                row_parts.append(f"{p95:.4f}".rjust(col_width))
                for comp in components:
                    z = zscores[comp]
                    row_parts.append(("N/A" if np.isnan(z) else f"{z:+.4f}").rjust(col_width))

                line = " | ".join(row_parts)
                log_file.write(f"{line}\n")
                total_lines += 1
                total_chars += len(line)

            # Summary
            log_file.write(f"{separator}\n")
            log_file.write(f"  Total data lines: {total_lines}\n")
            log_file.write(f"  Total chars (data rows): {total_chars}\n")
            log_file.write(f"  Avg line length: {total_chars / max(total_lines, 1):.1f} chars\n")
            log_file.write(f"  Table header length: {len(table_header)} chars\n\n")
            sections += 1

    return sections


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    metrics_cache: dict[str, pd.DataFrame] = {}

    log_path = OUT_DIR / "telecom_metric_stats.log"

    with open(log_path, "w") as log_file:
        log_file.write("=" * 100 + "\n")
        log_file.write("OpenRCA Telecom - 1-min Interval Peer Comparison Statistics\n")
        log_file.write(f"Generated: {datetime.now(timezone.utc).isoformat()}\n")
        log_file.write("Stats: Per-component value, Peer Mean/Std, P5, P90, P95, Z-score\n")
        log_file.write("=" * 100 + "\n")

        for query in QUERIES:
            date_dir = query["date_dir"]
            if date_dir not in metrics_cache:
                metrics_cache[date_dir] = load_metrics(date_dir)

            all_metrics = metrics_cache[date_dir]
            if all_metrics.empty:
                log_file.write(f"\n[{query['id']}] WARNING: No metric data for {date_dir}\n")
                continue

            window_df = filter_window(all_metrics, query["start"], query["end"])

            log_file.write(f"\n{'#'*100}\n")
            log_file.write(f"# QUERY: {query['id']} | {query['reason']} @ {query['root_cause']}\n")
            log_file.write(f"# Window: {query['start']} ~ {query['end']} (UTC)\n")
            log_file.write(f"# Fault:  {query['fault_time']} (UTC)\n")
            log_file.write(f"# Data points in window: {len(window_df)}\n")
            log_file.write(f"{'#'*100}\n")

            if window_df.empty:
                log_file.write("  WARNING: No data in query window!\n")
                continue

            n = write_query_log(query, window_df, log_file)
            log_file.write(f"\n  >> {n} KPI section(s) written for {query['id']}\n")

        # File-level summary
        log_file.write(f"\n{'='*100}\n")
        log_file.write("END OF REPORT\n")
        log_file.write(f"{'='*100}\n")

    # Print file stats
    content = log_path.read_text()
    lines = content.split("\n")
    print(f"Log saved: {log_path}")
    print(f"  Total lines: {len(lines)}")
    print(f"  Total chars: {len(content)}")
    print(f"  File size: {log_path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
