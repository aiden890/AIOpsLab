"""Render earliest anomalous caller-callee trace paths from a trace_span.csv file.

This is a standalone script for quickly testing the path-centric trace
visualization directly from a CSV, without running the full RCA pipeline.

Examples:
    python scripts/render_trace_anomalous_paths_from_csv.py
    python scripts/render_trace_anomalous_paths_from_csv.py --window-minutes 30
    python scripts/render_trace_anomalous_paths_from_csv.py --output tmp/task_1-13_trace_paths.png
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


DEFAULT_INPUT = Path(
    "/Users/gyuri/Documents/rcagent/AIOpsLab/"
    "prefiltered_telemetry/task_1-13/static-telecom/traces/trace_span.csv"
)


def normalize_trace_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize trace schema to include startTime, cmdb_id, dsName, elapsedTime, success."""
    if "dsName" in df.columns and "startTime" in df.columns:
        return df

    parent_col = None
    if "parent_id" in df.columns:
        parent_col = "parent_id"
    elif "parent_span" in df.columns:
        parent_col = "parent_span"

    if "span_id" not in df.columns or parent_col is None:
        return df

    df = df.copy()
    if "startTime" not in df.columns and "timestamp" in df.columns:
        df["startTime"] = df["timestamp"]
    if "elapsedTime" not in df.columns and "duration" in df.columns:
        df["elapsedTime"] = df["duration"]
    if "success" not in df.columns:
        if "status_code" in df.columns:
            df["success"] = df["status_code"].astype(str).str.strip().str.lower().isin(
                ["0", "200", "ok"]
            )
        else:
            df["success"] = True
    if parent_col != "parent_id":
        df["parent_id"] = df[parent_col]

    span_cmdb = df.set_index("span_id")["cmdb_id"].to_dict()
    df["_parent_cmdb"] = df["parent_id"].map(span_cmdb)
    edges = df[df["_parent_cmdb"].notna()].copy()
    if edges.empty:
        df["dsName"] = df["cmdb_id"]
        return df.drop(columns=["_parent_cmdb"], errors="ignore")

    edges["dsName"] = edges["cmdb_id"]
    edges["cmdb_id"] = edges["_parent_cmdb"]
    return edges.drop(columns=["_parent_cmdb"], errors="ignore")


def load_trace_csv(path: Path, window_minutes: int | None = None) -> pd.DataFrame:
    """Load trace CSV, parse time, and optionally trim to the last N minutes."""
    df = pd.read_csv(path, low_memory=False)
    if df.empty:
        return df

    df = normalize_trace_schema(df)
    ts_col = "startTime" if "startTime" in df.columns else "timestamp"
    ts = df[ts_col].copy()
    if pd.to_numeric(ts, errors="coerce").median() > 1e12:
        ts = pd.to_numeric(ts, errors="coerce") / 1000.0
    df["datetime"] = pd.to_datetime(ts, unit="s", utc=True, errors="coerce")
    df = df[df["datetime"].notna()].copy()
    if df.empty:
        return df

    df["bucket"] = df["datetime"].dt.floor("1min")
    if "success" in df.columns:
        if df["success"].dtype == object:
            df["success_bool"] = df["success"].astype(str).str.strip().str.lower().isin(
                ["true", "0", "200", "ok"]
            )
        else:
            df["success_bool"] = df["success"].astype(bool)
    else:
        df["success_bool"] = True

    if window_minutes and window_minutes > 0:
        end_dt = df["datetime"].max()
        start_dt = end_dt - pd.Timedelta(minutes=int(window_minutes))
        df = df[df["datetime"] >= start_dt].copy()

    return df


def _extract_edge_rows_id_pid(df: pd.DataFrame) -> pd.DataFrame:
    """Extract caller->callee edge rows from id/pid (RPC: parent cmdb_id -> child cmdb_id).

    child.pid = parent.id -> caller=parent.cmdb_id, callee=child.cmdb_id.
    Returns DataFrame with columns: caller, callee, bucket, elapsedTime, success_bool.
    """
    id_col = "id" if "id" in df.columns else ("span_id" if "span_id" in df.columns else None)
    pid_col = "pid" if "pid" in df.columns else ("parent_id" if "parent_id" in df.columns else ("parent_span" if "parent_span" in df.columns else None))
    if id_col is None or pid_col is None or "cmdb_id" not in df.columns:
        return pd.DataFrame()

    df = df.copy()
    df["_id"] = df[id_col].astype(str)
    df["_pid"] = df[pid_col].fillna("").astype(str)
    children = df[df["_pid"].str.strip() != ""].copy()
    if children.empty:
        return pd.DataFrame()

    parent_map = df.set_index("_id")["cmdb_id"].to_dict()
    children["caller"] = children["_pid"].map(parent_map)
    children["callee"] = children["cmdb_id"].astype(str).str.strip()
    edges = children[children["caller"].notna() & (children["caller"] != children["callee"])].copy()
    if edges.empty:
        return pd.DataFrame()

    out = edges[["caller", "callee", "bucket"]].copy()
    dur_col = "elapsedTime" if "elapsedTime" in edges.columns else "duration"
    out["elapsedTime"] = edges[dur_col].astype(float) if dur_col in edges.columns else 0.0
    out["success_bool"] = edges["success_bool"] if "success_bool" in edges.columns else True
    return out


def _extract_edge_rows_dsname(df: pd.DataFrame) -> pd.DataFrame:
    """Extract caller->callee edge rows from cmdb_id + dsName (JDBC: docker->db).

    Telecom JDBC: cmdb_id=caller, dsName=callee.
    Returns DataFrame with columns: caller, callee, bucket, elapsedTime, success_bool.
    """
    if "cmdb_id" not in df.columns or "dsName" not in df.columns:
        return pd.DataFrame()

    df = df.copy()
    df["caller"] = df["cmdb_id"].astype(str).str.strip()
    df["callee"] = df["dsName"].astype(str).str.strip()
    edges = df[(df["callee"] != "") & (df["caller"] != df["callee"])].copy()
    if edges.empty:
        return pd.DataFrame()

    out = edges[["caller", "callee", "bucket"]].copy()
    dur_col = "elapsedTime" if "elapsedTime" in edges.columns else "duration"
    out["elapsedTime"] = edges[dur_col].astype(float) if dur_col in edges.columns else 0.0
    out["success_bool"] = edges["success_bool"] if "success_bool" in edges.columns else True
    return out


def build_trace_edge_metric_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate trace rows into per-minute caller->callee edge metrics.

    Uses both id/pid (RPC, docker-docker) and cmdb_id-dsName (JDBC, docker-db) for Telecom.
    For Bank/Market (normalized edge schema), uses only cmdb_id-dsName to avoid duplicates.
    """
    if df.empty:
        return df

    edge_rows: list[pd.DataFrame] = []
    is_telecom = "id" in df.columns and "pid" in df.columns

    if is_telecom:
        # Telecom: use both id/pid (RPC) and cmdb_id-dsName (JDBC)
        id_pid_edges = _extract_edge_rows_id_pid(df)
        if not id_pid_edges.empty:
            edge_rows.append(id_pid_edges)
        dsname_edges = _extract_edge_rows_dsname(df)
        if not dsname_edges.empty:
            edge_rows.append(dsname_edges)
    else:
        # Bank/Market: normalized schema already has cmdb_id=caller, dsName=callee
        dsname_edges = _extract_edge_rows_dsname(df)
        if not dsname_edges.empty:
            edge_rows.append(dsname_edges)

    if not edge_rows:
        # Fallback: legacy cmdb_id/dsName per row (Bank/Market after normalize)
        edge_df = df.copy()
        edge_df["caller"] = edge_df["cmdb_id"].fillna("").astype(str).str.strip()
        edge_df["callee"] = edge_df["dsName"].fillna("").astype(str).str.strip()
        edge_df = edge_df[(edge_df["caller"] != "") & (edge_df["callee"] != "")]
        if edge_df.empty:
            return pd.DataFrame()
        edge_rows = [edge_df]

    combined = pd.concat(edge_rows, ignore_index=True)
    if "elapsedTime" not in combined.columns:
        combined["elapsedTime"] = 0.0
    if "success_bool" not in combined.columns:
        combined["success_bool"] = True

    return (
        combined
        .groupby(["caller", "callee", "bucket"], as_index=False)
        .agg(
            latency_p50=("elapsedTime", "median"),
            error_rate=("success_bool", lambda s: (1 - s.mean()) * 100),
            volume=("success_bool", "size"),
        )
        .sort_values(["caller", "callee", "bucket"])
    )


def detect_trace_metric_onset(
    edge_metric_df: pd.DataFrame,
    value_col: str,
    metric_kind: str,
    *,
    baseline_buckets: int = 5,
    sustain_buckets: int = 2,
    min_edge_volume: int = 20,
) -> dict | None:
    """Detect the earliest sustained shift in one edge metric."""
    if edge_metric_df.empty:
        return None

    series_df = edge_metric_df.sort_values("bucket").reset_index(drop=True)
    n_rows = len(series_df)
    if n_rows < baseline_buckets + sustain_buckets:
        return None

    values = series_df[value_col].astype(float)
    volumes = series_df["volume"].fillna(0).astype(float)
    buckets = series_df["bucket"]

    for idx in range(baseline_buckets, n_rows - sustain_buckets + 1):
        history = values.iloc[idx - baseline_buckets:idx]
        if history.empty or history.isna().any():
            continue

        future_vals = values.iloc[idx:idx + sustain_buckets]
        future_vols = volumes.iloc[idx:idx + sustain_buckets]
        if len(future_vals) < sustain_buckets:
            continue

        baseline = float(history.median())
        peak = float(future_vals.max())
        trough = float(future_vals.min())
        if metric_kind == "latency":
            if (future_vols < min_edge_volume).any():
                continue
            threshold = max(baseline * 1.8, baseline + 40.0)
            if baseline <= 1.0:
                threshold = max(threshold, 40.0)
            if all(float(v) >= threshold for v in future_vals):
                return {
                    "metric": metric_kind,
                    "onset": buckets.iloc[idx],
                    "baseline": baseline,
                    "peak": peak,
                    "threshold": threshold,
                }
        elif metric_kind == "error_rate":
            if (future_vols < min_edge_volume).any():
                continue
            threshold = max(baseline * 3.0, baseline + 10.0, 5.0)
            if all(float(v) >= threshold for v in future_vals):
                return {
                    "metric": metric_kind,
                    "onset": buckets.iloc[idx],
                    "baseline": baseline,
                    "peak": peak,
                    "threshold": threshold,
                }
        elif metric_kind == "volume_drop":
            if baseline < min_edge_volume:
                continue
            threshold = min(baseline * 0.5, baseline - 10.0)
            threshold = max(threshold, 0.0)
            if all(float(v) <= threshold for v in future_vals):
                return {
                    "metric": metric_kind,
                    "onset": buckets.iloc[idx],
                    "baseline": baseline,
                    "trough": trough,
                    "threshold": threshold,
                    "drop_pct": max(0.0, (baseline - trough) / max(baseline, 1.0) * 100.0),
                }

    return None


def detect_trace_edge_anomalies(
    edge_metric_df: pd.DataFrame,
    *,
    baseline_buckets: int = 5,
    sustain_buckets: int = 2,
    min_edge_volume: int = 20,
) -> list[dict]:
    """Detect earliest anomalous caller->callee edges from latency, error, or volume shifts."""
    if edge_metric_df.empty:
        return []

    anomalies: list[dict] = []
    grouped = edge_metric_df.groupby(["caller", "callee"], sort=True)
    for (caller, callee), group in grouped:
        if not caller or not callee:
            continue

        latency_info = detect_trace_metric_onset(
            group,
            "latency_p50",
            "latency",
            baseline_buckets=baseline_buckets,
            sustain_buckets=sustain_buckets,
            min_edge_volume=min_edge_volume,
        )
        error_info = detect_trace_metric_onset(
            group,
            "error_rate",
            "error_rate",
            baseline_buckets=baseline_buckets,
            sustain_buckets=sustain_buckets,
            min_edge_volume=min_edge_volume,
        )
        volume_info = detect_trace_metric_onset(
            group,
            "volume",
            "volume_drop",
            baseline_buckets=baseline_buckets,
            sustain_buckets=sustain_buckets,
            min_edge_volume=min_edge_volume,
        )
        if not latency_info and not error_info and not volume_info:
            continue

        metric_infos = {
            "latency": latency_info,
            "error_rate": error_info,
            "volume_drop": volume_info,
        }
        onset_candidates = [x["onset"] for x in metric_infos.values() if x is not None]
        first_onset = min(onset_candidates)
        earliest_metrics = [
            name for name, info in metric_infos.items()
            if info is not None
            and abs((info["onset"] - first_onset).total_seconds()) / 60.0 <= 1.5
        ]
        # Single: latency, error_rate, volume_drop. Multiple: "latency+error_rate", etc.
        dominant_metric = "+".join(sorted(earliest_metrics)) if earliest_metrics else "mixed"

        latency_score = 0.0
        if latency_info:
            latency_score = (
                max(latency_info["peak"] - latency_info["baseline"], 0.0)
                / max(latency_info["baseline"], 1.0)
            )
        error_score = 0.0
        if error_info:
            error_score = (
                max(error_info["peak"] - error_info["baseline"], 0.0)
                / max(error_info["baseline"], 1.0)
            )
        volume_score = 0.0
        if volume_info:
            volume_score = max(
                volume_info["baseline"] - volume_info["trough"], 0.0
            ) / max(volume_info["baseline"], 1.0)

        anomalies.append(
            {
                "edge_id": f"{caller}->{callee}",
                "caller": caller,
                "callee": callee,
                "first_onset": first_onset,
                "dominant_metric": dominant_metric,
                "latency_info": latency_info,
                "error_info": error_info,
                "volume_info": volume_info,
                "series": group.sort_values("bucket").reset_index(drop=True),
                "score": latency_score + error_score + volume_score,
            }
        )

    return sorted(
        anomalies,
        key=lambda item: (item["first_onset"], -item["score"], item["edge_id"]),
    )


def select_trace_path_subgraph(
    anomalies: list[dict],
    *,
    top_k_paths: int = 5,
    onset_slack_minutes: int = 3,
    path_slack_minutes: int = 5,
) -> list[dict]:
    """Grow a connected anomalous subgraph around the earliest anomalous edges."""
    if not anomalies:
        return []

    earliest = anomalies[0]["first_onset"]
    seed_cutoff = earliest + pd.Timedelta(minutes=onset_slack_minutes)
    seeds = [item for item in anomalies if item["first_onset"] <= seed_cutoff]
    seeds = seeds[:max(1, top_k_paths)]

    outgoing: dict[str, list[dict]] = {}
    incoming: dict[str, list[dict]] = {}
    for item in anomalies:
        outgoing.setdefault(item["caller"], []).append(item)
        incoming.setdefault(item["callee"], []).append(item)

    selected: dict[str, dict] = {item["edge_id"]: item for item in seeds}
    queue = list(seeds)
    while queue:
        current = queue.pop(0)
        current_time = current["first_onset"]
        neighbors = outgoing.get(current["callee"], []) + incoming.get(current["caller"], [])
        for candidate in neighbors:
            if candidate["edge_id"] in selected:
                continue
            delta_min = abs(
                (candidate["first_onset"] - current_time).total_seconds()
            ) / 60.0
            if delta_min > path_slack_minutes:
                continue
            selected[candidate["edge_id"]] = candidate
            queue.append(candidate)

    selected_edges = list(selected.values())
    selected_edges.sort(
        key=lambda item: (item["first_onset"], -item["score"], item["edge_id"])
    )
    return selected_edges[: max(top_k_paths * 3, 8)]


def compute_trace_path_layout(selected_edges: list[dict]) -> dict[str, tuple[float, float]]:
    """Layout nodes left-to-right by causal depth."""
    nodes = sorted(
        {item["caller"] for item in selected_edges} | {item["callee"] for item in selected_edges}
    )
    if not nodes:
        return {}

    indegree = {node: 0 for node in nodes}
    outgoing_nodes: dict[str, list[str]] = {node: [] for node in nodes}
    for item in selected_edges:
        caller = item["caller"]
        callee = item["callee"]
        if callee not in outgoing_nodes[caller]:
            outgoing_nodes[caller].append(callee)
            indegree[callee] += 1

    depth = {node: 0 for node in nodes}
    queue = [node for node in nodes if indegree[node] == 0]
    visited = set(queue)
    while queue:
        node = queue.pop(0)
        for nxt in outgoing_nodes.get(node, []):
            depth[nxt] = max(depth[nxt], depth[node] + 1)
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
                visited.add(nxt)

    for node in nodes:
        if node not in visited:
            depth[node] = max(depth.values(), default=0)

    layers: dict[int, list[str]] = {}
    for node in nodes:
        layers.setdefault(depth[node], []).append(node)

    positions: dict[str, tuple[float, float]] = {}
    for layer_idx, layer_nodes in sorted(layers.items()):
        layer_nodes = sorted(layer_nodes)
        count = len(layer_nodes)
        for row_idx, node in enumerate(layer_nodes):
            y = (count - 1) / 2.0 - row_idx
            positions[node] = (layer_idx * 3.4, y * 1.7)
    return positions


# 7 combinations: L, E, V, L+E, L+V, V+E, L+V+E
_METRIC_COLORS = {
    "latency": "#1f77b4",
    "error_rate": "#d62728",
    "volume_drop": "#ff7f0e",
    "error_rate+latency": "#e377c2",
    "latency+volume_drop": "#2ca02c",
    "error_rate+volume_drop": "#bcbd22",
    "error_rate+latency+volume_drop": "#9467bd",
}

_METRIC_LABELS = {
    "latency": "latency",
    "error_rate": "error rate",
    "volume_drop": "volume drop",
    "error_rate+latency": "latency + error rate",
    "latency+volume_drop": "latency + volume drop",
    "error_rate+volume_drop": "error rate + volume drop",
    "error_rate+latency+volume_drop": "latency + error rate + volume drop",
}


def edge_plot_color(dominant_metric: str) -> str:
    return _METRIC_COLORS.get(dominant_metric, "#7f7f7f")


def edge_summary_label(item: dict) -> str:
    return f"{item['caller']} -> {item['callee']}"


def edge_graph_label(item: dict) -> str:
    onset = item["first_onset"].strftime("%H:%M")
    parts = [onset]
    if item.get("latency_info"):
        parts.append(f"L {item['latency_info']['peak']:.0f}ms")
    if item.get("error_info"):
        parts.append(f"E {item['error_info']['peak']:.0f}%")
    if item.get("volume_info"):
        parts.append(f"V -{item['volume_info']['drop_pct']:.0f}%")
    return "\n".join(parts)


def plot_trace_anomalous_path_figure(
    selected_edges: list[dict],
    out_path: Path,
    *,
    window_minutes: int,
) -> Path:
    """Render path graph plus edge-level latency/error/volume timelines."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    fig = plt.figure(figsize=(16, 15))
    grid = fig.add_gridspec(3, 2, height_ratios=[1.05, 1.0, 0.95])
    ax_graph = fig.add_subplot(grid[0, :])
    ax_lat = fig.add_subplot(grid[1, 0])
    ax_err = fig.add_subplot(grid[1, 1], sharex=ax_lat)
    ax_vol = fig.add_subplot(grid[2, :], sharex=ax_lat)

    positions = compute_trace_path_layout(selected_edges)
    ax_graph.set_axis_off()

    all_x = [pos[0] for pos in positions.values()] or [0.0]
    all_y = [pos[1] for pos in positions.values()] or [0.0]
    for idx, item in enumerate(selected_edges):
        sx, sy = positions[item["caller"]]
        tx, ty = positions[item["callee"]]
        color = edge_plot_color(item["dominant_metric"])
        rad = 0.08 if idx % 2 == 0 else -0.08
        ax_graph.annotate(
            "",
            xy=(tx - 0.35, ty),
            xytext=(sx + 0.35, sy),
            arrowprops={
                "arrowstyle": "->",
                "lw": 2.0,
                "color": color,
                "alpha": 0.9,
                "connectionstyle": f"arc3,rad={rad}",
            },
            zorder=1,
        )
        mx = (sx + tx) / 2.0
        my = (sy + ty) / 2.0 + (0.22 if idx % 2 == 0 else -0.22)
        ax_graph.text(
            mx,
            my,
            edge_graph_label(item),
            fontsize=8,
            ha="center",
            va="center",
            color=color,
            bbox={
                "boxstyle": "round,pad=0.18",
                "fc": "white",
                "ec": "none",
                "alpha": 0.85,
            },
            zorder=3,
        )

    for node, (x, y) in positions.items():
        ax_graph.text(
            x,
            y,
            node,
            ha="center",
            va="center",
            fontsize=10,
            bbox={
                "boxstyle": "round,pad=0.35",
                "fc": "#F7F8FA",
                "ec": "#4F5B67",
                "lw": 1.2,
            },
            zorder=4,
        )

    graph_legend = [
        Line2D([0], [0], color=c, lw=2, label=_METRIC_LABELS.get(k, k))
        for k, c in _METRIC_COLORS.items()
    ]
    ax_graph.legend(handles=graph_legend, loc="upper right", frameon=False, fontsize=7)
    ax_graph.set_title(
        f"Earliest anomalous caller-callee paths ({window_minutes}-minute trace window)",
        fontsize=13,
        fontweight="bold",
        pad=12,
    )
    ax_graph.set_xlim(min(all_x) - 1.3, max(all_x) + 1.3)
    ax_graph.set_ylim(min(all_y) - 1.4, max(all_y) + 1.4)

    for item in selected_edges:
        color = edge_plot_color(item["dominant_metric"])
        label = edge_summary_label(item)
        series = item["series"]
        ax_lat.plot(
            series["bucket"],
            series["latency_p50"],
            label=label,
            color=color,
            linewidth=1.8,
            alpha=0.9,
        )
        ax_err.plot(
            series["bucket"],
            series["error_rate"],
            label=label,
            color=color,
            linewidth=1.8,
            alpha=0.9,
        )
        ax_vol.plot(
            series["bucket"],
            series["volume"],
            label=label,
            color=color,
            linewidth=1.8,
            alpha=0.9,
        )
        if item.get("latency_info"):
            ax_lat.axvline(
                item["latency_info"]["onset"],
                color=color,
                linestyle=":",
                alpha=0.35,
                linewidth=1.0,
            )
        if item.get("error_info"):
            ax_err.axvline(
                item["error_info"]["onset"],
                color=color,
                linestyle=":",
                alpha=0.35,
                linewidth=1.0,
            )
        if item.get("volume_info"):
            ax_vol.axvline(
                item["volume_info"]["onset"],
                color=color,
                linestyle=":",
                alpha=0.35,
                linewidth=1.0,
            )

    ax_lat.set_title("Edge latency (p50) over time", fontsize=11, fontweight="bold")
    ax_lat.set_ylabel("Latency (ms)")
    ax_lat.grid(True, alpha=0.3)
    ax_lat.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_lat.legend(loc="upper left", fontsize=7, ncol=2)

    ax_err.set_title("Edge error rate over time", fontsize=11, fontweight="bold")
    ax_err.set_ylabel("Error rate (%)")
    ax_err.set_xlabel("Time (UTC)")
    ax_err.grid(True, alpha=0.3)
    ax_err.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_err.legend(loc="upper left", fontsize=7, ncol=2)

    ax_vol.set_title("Edge span volume over time", fontsize=11, fontweight="bold")
    ax_vol.set_ylabel("Span count")
    ax_vol.set_xlabel("Time (UTC)")
    ax_vol.grid(True, alpha=0.3)
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax_vol.legend(loc="upper left", fontsize=7, ncol=2)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    plt.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="trace_span.csv path")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("/Users/gyuri/Documents/rcagent/AIOpsLab/tmp/trace_anomalous_paths_task_1_5.png"),
        help="PNG output path",
    )
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--top-k-paths", type=int, default=5)
    parser.add_argument("--min-edge-volume", type=int, default=20)
    parser.add_argument("--onset-slack-minutes", type=int, default=3)
    parser.add_argument("--sustain-buckets", type=int, default=2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    df = load_trace_csv(args.input, window_minutes=args.window_minutes)
    if df.empty:
        raise SystemExit(f"No trace rows found in {args.input}")

    edge_metric_df = build_trace_edge_metric_frame(df)
    if edge_metric_df.empty:
        raise SystemExit("No caller-callee trace edges found in the selected window")

    anomalies = detect_trace_edge_anomalies(
        edge_metric_df,
        sustain_buckets=args.sustain_buckets,
        min_edge_volume=args.min_edge_volume,
    )
    if not anomalies:
        raise SystemExit(
            "No anomalous caller-callee paths found. "
            f"Checked last {args.window_minutes} minutes with min_edge_volume={args.min_edge_volume}."
        )

    selected_edges = select_trace_path_subgraph(
        anomalies,
        top_k_paths=args.top_k_paths,
        onset_slack_minutes=args.onset_slack_minutes,
        path_slack_minutes=max(args.onset_slack_minutes + 2, 5),
    )
    if not selected_edges:
        raise SystemExit("Trace anomalies were detected, but no connected path subgraph could be formed.")

    out_path = plot_trace_anomalous_path_figure(
        selected_edges,
        args.output,
        window_minutes=args.window_minutes,
    )
    print(f"Saved graph to {out_path}")
    print("Earliest anomalous edges:")
    for item in selected_edges[: min(len(selected_edges), 8)]:
        onset = item["first_onset"].strftime("%Y-%m-%d %H:%M:%S")
        print(
            f"- {item['caller']} -> {item['callee']} | onset={onset} | mode={item['dominant_metric']}"
        )


if __name__ == "__main__":
    main()
