"""Render a standalone localization dependency graph from a hardcoded case dict.

This script reproduces the `db-v3/task_1-5` localization dependency view
without needing to run the full RCA pipeline.

Usage:
    python scripts/render_localization_dependency_case.py
    python scripts/render_localization_dependency_case.py --output tmp/db_v3_case.png
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOCALIZATION_DEP_CLUSTER_WINDOW_MINUTES = 10
LOCALIZATION_DEP_RELATION_ORDER = {"call": 0, "shared": 1, "deploy": 2}
LOCALIZATION_DEP_RELATION_COLORS = {
    "call": "#4A90D9",
    "shared": "#8E44AD",
    "deploy": "#E67E22",
}
LOCALIZATION_DEP_CLUSTER_GAP = 4.2
LOCALIZATION_DEP_BOX_WIDTH = 2.6
LOCALIZATION_DEP_BOX_HEIGHT = 0.62
LOCALIZATION_DEP_INNER_COL_GAP = 3.4
LOCALIZATION_DEP_GROUP_ORDER = ["os", "node", "docker", "service", "db", "redis", "other"]


LOCALIZATION_CASE = {
    "case_name": "openrca_telecom db-v3 task_1-5",
    "dataset_key": "openrca_telecom",
    "cluster_window_minutes": 10,
    "items": [
        {
            "id": "db_003_trace_error",
            "component": "db_003",
            "time": "2026-03-17 21:46:00",
            "kpi": "Trace Error Rate",
            "severity": 92.0,
        },
        {
            "id": "db_003_tnsping_result_time",
            "component": "db_003",
            "time": "2026-03-17 21:46:00",
            "kpi": "tnsping_result_time",
            "severity": 90.0,
        },
        {
            "id": "db_003_on_off_state",
            "component": "db_003",
            "time": "2026-03-17 21:45:00",
            "kpi": "On_Off_State",
            "severity": 95.0,
        },
        {
            "id": "docker_005_trace_error",
            "component": "docker_005",
            "time": "2026-03-17 21:46:00",
            "kpi": "Trace Error Rate (%)",
            "severity": 86.0,
        },
        {
            "id": "docker_008_trace_error",
            "component": "docker_008",
            "time": "2026-03-17 21:46:00",
            "kpi": "Trace Error Rate (%)",
            "severity": 85.0,
        },
        {
            "id": "db_003_sess_connect",
            "component": "db_003",
            "time": "2026-03-17 21:50:00",
            "kpi": "Sess_Connect",
            "severity": 84.0,
        },
        {
            "id": "docker_004_trace_error",
            "component": "docker_004",
            "time": "2026-03-17 21:46:00",
            "kpi": "Trace Error Rate (%)",
            "severity": 83.0,
        },
        {
            "id": "docker_004_container_cpu",
            "component": "docker_004",
            "time": "2026-03-17 21:53:00",
            "kpi": "container_cpu_used",
            "severity": 82.0,
        },
        {
            "id": "os_022_trace_latency",
            "component": "os_022",
            "time": "2026-03-17 21:55:00",
            "kpi": "Trace Latency (p50)",
            "severity": 80.0,
        },
        {
            "id": "os_012_disk_io_util",
            "component": "os_012",
            "time": "2026-03-17 21:45:00",
            "kpi": "Disk_io_util",
            "severity": 81.0,
        },
        {
            "id": "os_004_disk_io_util",
            "component": "os_004",
            "time": "2026-03-17 21:45:00",
            "kpi": "Disk_io_util",
            "severity": 55.0,
        },
        {
            "id": "docker_001_container_cpu",
            "component": "docker_001",
            "time": "2026-03-17 22:00:00",
            "kpi": "container_cpu_used",
            "severity": 82.0,
        },
    ],
}


def load_get_graphs():
    """Load `get_graphs()` directly from `graph.py` without importing the package."""
    graph_path = REPO_ROOT / "clients" / "tree_traversal" / "graph.py"
    spec = importlib.util.spec_from_file_location("standalone_tree_graph", graph_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load graph module from {graph_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.get_graphs


GET_GRAPHS = load_get_graphs()


def parse_time(time_str: str | None) -> datetime | None:
    """Parse localization time, accepting full timestamps and HH:MM[:SS]."""
    if not time_str:
        return None

    s = str(time_str).strip()
    s = re.sub(r"\s*(UTC|Z)$", "", s, flags=re.I)
    for prefix in ("approx ", "approximately ", "~", "around ", "at "):
        if s.lower().startswith(prefix):
            s = s[len(prefix):].strip()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    return datetime(2026, 3, 17, hour, minute, second, tzinfo=timezone.utc)


def finalize_cluster(items: list[dict], min_dt: datetime | None, max_dt: datetime | None) -> dict:
    """Sort cluster items and return normalized cluster payload."""
    return {
        "items": sorted(items, key=lambda it: (-it["severity"], it["component"], it["time"])),
        "min_dt": min_dt,
        "max_dt": max_dt,
    }


def cluster_items(case: dict) -> list[dict]:
    """Group localization items so each cluster spans at most N minutes."""
    items = []
    for raw in case["items"]:
        item = dict(raw)
        item["dt"] = parse_time(item.get("time"))
        items.append(item)

    parseable = sorted(
        (item for item in items if item["dt"] is not None),
        key=lambda it: (it["dt"], -it["severity"], it["component"]),
    )
    unknown_time = [item for item in items if item["dt"] is None]

    clusters: list[dict] = []
    current: list[dict] = []
    current_min: datetime | None = None
    current_max: datetime | None = None
    max_span = timedelta(minutes=case.get("cluster_window_minutes", 10))

    for item in parseable:
        item_dt = item["dt"]
        if item_dt is None:
            continue
        if not current:
            current = [item]
            current_min = item_dt
            current_max = item_dt
            continue

        next_min = min(current_min, item_dt)
        next_max = max(current_max, item_dt)
        if next_max - next_min <= max_span:
            current.append(item)
            current_min = next_min
            current_max = next_max
        else:
            clusters.append(finalize_cluster(current, current_min, current_max))
            current = [item]
            current_min = item_dt
            current_max = item_dt

    if current:
        clusters.append(finalize_cluster(current, current_min, current_max))

    for item in unknown_time:
        clusters.append(finalize_cluster([item], None, None))

    return clusters


def component_aliases(component: str | None) -> list[str]:
    """Return aliases for dependency lookups while preserving concrete IDs."""
    comp = (component or "").strip()
    if not comp:
        return []

    aliases: list[str] = []
    for candidate in (comp, comp.split(".", 1)[-1] if "." in comp else ""):
        candidate = candidate.strip()
        if candidate and candidate not in aliases:
            aliases.append(candidate)

    leaf = aliases[-1]
    if (
        leaf
        and re.match(r"^[A-Za-z][A-Za-z0-9_]*-\d+$", leaf)
        and not re.match(r"^node-\d+$", leaf)
    ):
        service = re.sub(r"-\d+$", "", leaf)
        if service and service not in aliases:
            aliases.append(service)

    return aliases


def get_relation(source_component: str, target_component: str, graphs: dict[str, dict]) -> str | None:
    """Return the directed dependency type from source -> target, if any."""
    source_aliases = component_aliases(source_component)
    target_aliases = set(component_aliases(target_component))
    if not source_aliases or not target_aliases:
        return None

    call_graph = graphs.get("call_graph") or {}
    deployment_graph = graphs.get("deployment_graph") or {}
    shared_graph = graphs.get("shared_resource_graph") or {}

    for alias in source_aliases:
        for callee in call_graph.get(alias, []):
            if callee in target_aliases:
                return "call"

        for deployed in deployment_graph.get(alias, []):
            if deployed in target_aliases:
                return "deploy"

        for resource, callers in shared_graph.items():
            if alias in callers and resource in target_aliases:
                return "shared"

    return None


def build_edges(case: dict, clusters: list[dict]) -> list[dict]:
    """Connect localized anomalies when a directed dependency exists."""
    graphs = GET_GRAPHS(case["dataset_key"])
    items = [item for cluster in clusters for item in cluster["items"]]

    cluster_index_by_id: dict[str, int] = {}
    for idx, cluster in enumerate(clusters):
        for item in cluster["items"]:
            cluster_index_by_id[item["id"]] = idx

    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for source in items:
        for target in items:
            if source["id"] == target["id"]:
                continue
            relation = get_relation(source["component"], target["component"], graphs)
            if not relation:
                continue
            key = (source["id"], target["id"], relation)
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                {
                    "source_id": source["id"],
                    "target_id": target["id"],
                    "relation": relation,
                    "source_cluster": cluster_index_by_id.get(source["id"], -1),
                    "target_cluster": cluster_index_by_id.get(target["id"], -1),
                }
            )

    return sorted(
        edges,
        key=lambda edge: (
            edge["source_cluster"],
            edge["target_cluster"],
            LOCALIZATION_DEP_RELATION_ORDER.get(edge["relation"], 99),
            edge["source_id"],
            edge["target_id"],
        ),
    )


def component_group(component: str) -> str:
    """Return a coarse component family for layout."""
    match = re.match(r"^([A-Za-z]+)[_-]", component or "")
    if not match:
        return "other"
    group = match.group(1).lower()
    if group in {"os", "node", "docker", "service", "db", "redis"}:
        return group
    return "other"


def component_group_rank(component: str) -> tuple[int, str]:
    """Sort components by family first, then by name."""
    group = component_group(component)
    try:
        idx = LOCALIZATION_DEP_GROUP_ORDER.index(group)
    except ValueError:
        idx = len(LOCALIZATION_DEP_GROUP_ORDER)
    return idx, component


def aggregate_cluster(cluster_idx: int, cluster: dict) -> dict:
    """Aggregate raw localization items into one node per component."""
    grouped: dict[str, list[dict]] = {}
    for item in cluster["items"]:
        grouped.setdefault(item["component"], []).append(item)

    nodes: list[dict] = []
    for component, items in sorted(grouped.items(), key=lambda pair: component_group_rank(pair[0])):
        items = sorted(items, key=lambda it: (-it["severity"], it["time"], it["kpi"]))
        times = sorted({it["time"][11:16] if len(it["time"]) >= 16 else it["time"] for it in items})
        kpis = [it["kpi"] for it in items]
        nodes.append(
            {
                "id": f"cluster_{cluster_idx}:{component}",
                "component": component,
                "group": component_group(component),
                "severity": max(it["severity"] for it in items),
                "times": times,
                "kpis": kpis,
                "raw_ids": [it["id"] for it in items],
                "item_count": len(items),
            }
        )

    return {
        "min_dt": cluster["min_dt"],
        "max_dt": cluster["max_dt"],
        "nodes": nodes,
    }


def aggregate_clusters(clusters: list[dict]) -> list[dict]:
    """Aggregate all clusters into one node per component."""
    return [aggregate_cluster(idx, cluster) for idx, cluster in enumerate(clusters)]


def build_component_edges(raw_clusters: list[dict], aggregated_clusters: list[dict], case: dict) -> list[dict]:
    """Build deduplicated dependency edges between aggregated component nodes."""
    raw_edges = build_edges(case, raw_clusters)
    raw_to_component: dict[str, tuple[int, str]] = {}
    for cluster_idx, cluster in enumerate(aggregated_clusters):
        for node in cluster["nodes"]:
            for raw_id in node["raw_ids"]:
                raw_to_component[raw_id] = (cluster_idx, node["component"])

    component_edges: list[dict] = []
    seen: set[tuple[int, str, int, str, str]] = set()
    for edge in raw_edges:
        source_info = raw_to_component.get(edge["source_id"])
        target_info = raw_to_component.get(edge["target_id"])
        if not source_info or not target_info:
            continue

        source_cluster, source_component = source_info
        target_cluster, target_component = target_info
        if source_cluster == target_cluster and source_component == target_component:
            continue

        key = (
            source_cluster,
            source_component,
            target_cluster,
            target_component,
            edge["relation"],
        )
        if key in seen:
            continue
        seen.add(key)
        component_edges.append(
            {
                "source_id": f"cluster_{source_cluster}:{source_component}",
                "target_id": f"cluster_{target_cluster}:{target_component}",
                "relation": edge["relation"],
                "source_cluster": source_cluster,
                "target_cluster": target_cluster,
            }
        )

    return sorted(
        component_edges,
        key=lambda edge: (
            edge["source_cluster"],
            edge["target_cluster"],
            LOCALIZATION_DEP_RELATION_ORDER.get(edge["relation"], 99),
            edge["source_id"],
            edge["target_id"],
        ),
    )


def format_cluster_title(cluster_idx: int, cluster: dict) -> str:
    """Format the cluster heading shown above each dependency group."""
    min_dt = cluster.get("min_dt")
    max_dt = cluster.get("max_dt")
    if min_dt and max_dt:
        if min_dt == max_dt:
            time_range = min_dt.strftime("%H:%M")
        else:
            time_range = f"{min_dt.strftime('%H:%M')} - {max_dt.strftime('%H:%M')}"
    else:
        time_range = "time unknown"
    return f"Cluster {cluster_idx + 1}\n{time_range}"


def node_face_color(severity: float) -> str:
    """Map anomaly severity to a node fill color."""
    if severity >= 80:
        return "#FDECEC"
    if severity >= 50:
        return "#FFF4E5"
    return "#ECF5FF"


def format_component_node_lines(node: dict) -> list[str]:
    """Build multi-line text for an aggregated component node."""
    lines = [node["component"]]
    if node["times"]:
        if len(node["times"]) == 1:
            lines.append(node["times"][0])
        else:
            lines.append(f"{node['times'][0]} - {node['times'][-1]}")

    for kpi in node["kpis"][:4]:
        text = kpi if len(kpi) <= 26 else kpi[:23] + "..."
        lines.append(f"- {text}")
    remaining = len(node["kpis"]) - 4
    if remaining > 0:
        lines.append(f"+ {remaining} more")
    return lines


def component_node_height(node: dict) -> float:
    """Return node height based on how many KPI lines are shown."""
    visible_kpis = min(len(node["kpis"]), 4)
    extra_line = 1 if len(node["kpis"]) > 4 else 0
    line_count = 2 + visible_kpis + extra_line
    return max(0.95, 0.28 * line_count + 0.10)


def compute_cluster_layout(cluster_idx: int, cluster: dict) -> dict:
    """Compute cluster bounds and per-node positions using component columns."""
    nodes = cluster["nodes"]
    group_to_nodes: dict[str, list[dict]] = {}
    for node in nodes:
        group_to_nodes.setdefault(node["group"], []).append(node)

    ordered_groups = [g for g in LOCALIZATION_DEP_GROUP_ORDER if g in group_to_nodes]
    if not ordered_groups:
        ordered_groups = ["other"]

    x_center = cluster_idx * LOCALIZATION_DEP_CLUSTER_GAP
    col_count = len(ordered_groups)
    col_offsets = {
        group: (idx - (col_count - 1) / 2.0) * LOCALIZATION_DEP_INNER_COL_GAP
        for idx, group in enumerate(ordered_groups)
    }

    positions: dict[str, tuple[float, float]] = {}
    node_sizes: dict[str, tuple[float, float]] = {}
    x_coords: list[float] = []
    y_mins: list[float] = []
    y_maxs: list[float] = []

    for group in ordered_groups:
        group_nodes = group_to_nodes[group]
        heights = [component_node_height(node) for node in group_nodes]
        gap = 0.35
        total_height = sum(heights) + gap * max(len(group_nodes) - 1, 0)
        cursor = total_height / 2.0

        for node, height in zip(group_nodes, heights):
            y = cursor - height / 2.0
            x = x_center + col_offsets[group]
            positions[node["id"]] = (x, y)
            node_sizes[node["id"]] = (LOCALIZATION_DEP_BOX_WIDTH, height)
            x_coords.append(x)
            y_mins.append(y - height / 2.0)
            y_maxs.append(y + height / 2.0)
            cursor -= height + gap

    x_min = min(x_coords) - LOCALIZATION_DEP_BOX_WIDTH / 2.0 - 0.55
    x_max = max(x_coords) + LOCALIZATION_DEP_BOX_WIDTH / 2.0 + 0.55
    y_min = min(y_mins) - 0.55
    y_max = max(y_maxs) + 0.55

    return {
        "title": format_cluster_title(cluster_idx, cluster),
        "positions": positions,
        "node_sizes": node_sizes,
        "bounds": (x_min, x_max, y_min, y_max),
        "groups": ordered_groups,
    }


def draw_cluster(
    ax,
    cluster_idx: int,
    cluster: dict,
    layout: dict,
    positions: dict[str, tuple[float, float]],
    node_sizes: dict[str, tuple[float, float]],
    fancy_box_patch,
) -> None:
    """Draw one clustered dependency group and its aggregated component nodes."""
    x_min, x_max, y_min, y_max = layout["bounds"]
    cluster_patch = fancy_box_patch(
        (x_min, y_min),
        x_max - x_min,
        y_max - y_min,
        boxstyle="round,pad=0.02,rounding_size=0.1",
        facecolor="#F7F8FA",
        edgecolor="#D9DEE7",
        linewidth=1.0,
        linestyle="--",
        zorder=0,
    )
    ax.add_patch(cluster_patch)
    ax.text(
        (x_min + x_max) / 2.0,
        y_max + 0.25,
        layout["title"],
        ha="center",
        va="bottom",
        fontsize=9,
        fontweight="bold",
        color="#333333",
    )

    for node in cluster["nodes"]:
        x, y = layout["positions"][node["id"]]
        width, height = layout["node_sizes"][node["id"]]
        positions[node["id"]] = (x, y)
        node_sizes[node["id"]] = (width, height)
        node_patch = fancy_box_patch(
            (x - width / 2.0, y - height / 2.0),
            width,
            height,
            boxstyle="round,pad=0.03,rounding_size=0.08",
            facecolor=node_face_color(node["severity"]),
            edgecolor="#4F5B67",
            linewidth=1.2,
            zorder=2,
        )
        ax.add_patch(node_patch)
        ax.text(
            x,
            y,
            "\n".join(format_component_node_lines(node)),
            ha="center",
            va="center",
            fontsize=7.5,
            color="#1F2933",
            zorder=3,
        )


def draw_edge(
    ax,
    edge_idx: int,
    edge: dict,
    positions: dict[str, tuple[float, float]],
    node_sizes: dict[str, tuple[float, float]],
) -> None:
    """Draw one directed dependency edge and its relation label."""
    source_pos = positions.get(edge["source_id"])
    target_pos = positions.get(edge["target_id"])
    if not source_pos or not target_pos:
        return

    sx, sy = source_pos
    tx, ty = target_pos
    source_width, _ = node_sizes[edge["source_id"]]
    target_width, _ = node_sizes[edge["target_id"]]
    relation = edge["relation"]
    color = LOCALIZATION_DEP_RELATION_COLORS.get(relation, "#7F8C8D")
    same_cluster = edge["source_cluster"] == edge["target_cluster"]
    rad = 0.12 if same_cluster else 0.04
    if same_cluster and edge_idx % 2:
        rad *= -1

    ax.annotate(
        "",
        xy=(tx - target_width / 2.0 + 0.10, ty),
        xytext=(sx + source_width / 2.0 - 0.10, sy),
        arrowprops={
            "arrowstyle": "->",
            "color": color,
            "lw": 1.4,
            "shrinkA": 4,
            "shrinkB": 4,
            "connectionstyle": f"arc3,rad={rad}",
        },
        zorder=1,
    )
    mid_x = (sx + tx) / 2.0
    mid_y = (sy + ty) / 2.0 + (0.12 if same_cluster else 0.06)
    ax.text(
        mid_x,
        mid_y,
        relation if not same_cluster else "",
        fontsize=7,
        color=color,
        ha="center",
        va="center",
        bbox={"boxstyle": "round,pad=0.18", "fc": "white", "ec": "none", "alpha": 0.85},
        zorder=4,
    )


def render_case(case: dict, output_path: Path) -> Path:
    """Render the standalone localization dependency graph PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import FancyBboxPatch

    raw_clusters = cluster_items(case)
    clusters = aggregate_clusters(raw_clusters)
    edges = build_component_edges(raw_clusters, clusters, case)
    layouts = [compute_cluster_layout(idx, cluster) for idx, cluster in enumerate(clusters)]

    max_nodes = max(len(cluster["nodes"]) for cluster in clusters)
    max_cols = max(len(layout["groups"]) for layout in layouts)
    fig_width = max(12.0, len(clusters) * max(5.8, max_cols * 3.0) + 2.0)
    fig_height = max(7.0, max_nodes * 1.5 + 2.5)
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))
    positions: dict[str, tuple[float, float]] = {}
    node_sizes: dict[str, tuple[float, float]] = {}

    x_mins: list[float] = []
    x_maxs: list[float] = []
    y_mins: list[float] = []
    y_maxs: list[float] = []
    for cluster_idx, (cluster, layout) in enumerate(zip(clusters, layouts)):
        draw_cluster(ax, cluster_idx, cluster, layout, positions, node_sizes, FancyBboxPatch)
        x_min, x_max, y_min, y_max = layout["bounds"]
        x_mins.append(x_min)
        x_maxs.append(x_max)
        y_mins.append(y_min)
        y_maxs.append(y_max)

    for edge_idx, edge in enumerate(edges):
        draw_edge(ax, edge_idx, edge, positions, node_sizes)

    if not edges:
        ax.text(
            0.5,
            0.03,
            "No directed dependency edges found among localization candidates.",
            transform=ax.transAxes,
            ha="center",
            va="bottom",
            fontsize=9,
            color="#666666",
        )

    legend_handles = [
        Line2D([0], [0], color=color, lw=2, label=label)
        for label, color in (
            ("call dependency", LOCALIZATION_DEP_RELATION_COLORS["call"]),
            ("shared resource dependency", LOCALIZATION_DEP_RELATION_COLORS["shared"]),
            ("deployment dependency", LOCALIZATION_DEP_RELATION_COLORS["deploy"]),
        )
    ]
    ax.legend(handles=legend_handles, loc="upper right", frameon=False, fontsize=8)
    ax.set_title(
        f"{case['case_name']}\nLocalization Dependency Graph ({case['cluster_window_minutes']}-minute clusters)",
        fontsize=12,
        pad=16,
    )
    ax.set_axis_off()
    ax.set_xlim(min(x_mins) - 1.0, max(x_maxs) + 1.0)
    ax.set_ylim(min(y_mins) - 1.0, max(y_maxs) + 1.4)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    try:
        fig.savefig(output_path, dpi=160, bbox_inches="tight")
    finally:
        plt.close(fig)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "tmp" / "localization_dependency_case_db_v3_task_1_5.png",
        help="PNG output path",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = render_case(LOCALIZATION_CASE, args.output)
    print(f"Saved graph to {output_path}")


if __name__ == "__main__":
    main()
