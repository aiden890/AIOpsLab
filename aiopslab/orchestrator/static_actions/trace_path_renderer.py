"""Dataset-specific trace anomalous path renderers.

Each renderer must keep the same input/output contract:
  input:  actions instance + namespace + rendering parameters
  output: string summary whose first line is the generated PNG path on success
"""

from __future__ import annotations

import os
import re
import pandas as pd

class BaseTracePathRenderer:
    """Common orchestration for trace anomalous path rendering."""

    @staticmethod
    def _events_from_anomalies(anomalies: list[dict]) -> list[dict]:
        def _to_time_str(v) -> str:
            try:
                return pd.to_datetime(v).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return str(v or "").strip()

        def _metric_events(metric_name: str, info: dict | None) -> list[dict]:
            if not isinstance(info, dict):
                return []
            episodes = list(info.get("episodes") or [])
            if not episodes:
                episodes = [info]
            out = []
            for ev in episodes:
                if not isinstance(ev, dict):
                    continue
                out.append(
                    {
                        "metric_name": metric_name,
                        "onset": _to_time_str(ev.get("onset")),
                        "end_time": _to_time_str(ev.get("end")),
                        "duration_minutes": ev.get("duration_minutes"),
                        "baseline": ev.get("baseline"),
                        "peak": ev.get("peak"),
                        "threshold": ev.get("threshold"),
                    }
                )
            return out

        rows: list[dict] = []
        for item in anomalies or []:
            caller = str(item.get("caller") or "").strip()
            callee = str(item.get("callee") or "").strip()
            if not caller or not callee:
                continue
            edge_id = str(item.get("edge_id") or f"{caller}->{callee}")
            metric_events = []
            metric_events.extend(_metric_events("latency_edge", item.get("latency_info")))
            metric_events.extend(_metric_events("error_rate", item.get("error_info")))
            metric_events.extend(_metric_events("network_gap", item.get("gap_info")))
            metric_events.extend(_metric_events("remote_process_time", item.get("remote_info")))
            for ev in metric_events:
                rows.append(
                    {
                        "edge_id": edge_id,
                        "caller": caller,
                        "callee": callee,
                        "anomaly_type": str(ev.get("metric_name") or "trace"),
                        "time": ev.get("onset") or "",
                        "end_time": ev.get("end_time") or "",
                        "duration_minutes": ev.get("duration_minutes"),
                        "metadata": {
                            "event": ev,
                            "dominant_metric": item.get("dominant_metric"),
                            "latency_info": item.get("latency_info"),
                            "error_info": item.get("error_info"),
                            "gap_info": item.get("gap_info"),
                            "remote_info": item.get("remote_info"),
                        },
                    }
                )
        return rows

    def build_edge_events(
        self,
        actions,
        namespace: str,
        *,
        window_minutes: int | None = 30,
        min_edge_volume: int = 20,
        sustain_buckets: int = 2,
    ) -> tuple[pd.DataFrame, list[dict], list[dict]]:
        if window_minutes is None:
            df = actions._load_trace_window(namespace)
        else:
            df = actions._load_trace_window_minutes(namespace, window_minutes=window_minutes)
        if df.empty:
            return pd.DataFrame(), [], []

        edge_metric_df = actions._build_trace_edge_metric_frame(df)
        if edge_metric_df.empty:
            return pd.DataFrame(), [], []
        gap_metric_df = actions._build_trace_edge_gap_frame(df)
        remote_metric_df = actions._build_trace_edge_remote_frame(df)

        anomalies = actions._detect_trace_edge_anomalies(
            edge_metric_df,
            gap_metric_df=gap_metric_df,
            remote_metric_df=remote_metric_df,
            sustain_buckets=sustain_buckets,
            min_edge_volume=min_edge_volume,
        )
        return edge_metric_df, anomalies, self._events_from_anomalies(anomalies)

    def render(
        self,
        actions,
        namespace: str,
        *,
        window_minutes: int = 30,
        top_k_paths: int = 5,
        min_edge_volume: int = 20,
        onset_slack_minutes: int = 3,
        sustain_buckets: int = 2,
    ) -> str:
        edge_metric_df, anomalies, _events = self.build_edge_events(
            actions,
            namespace,
            window_minutes=window_minutes,
            min_edge_volume=min_edge_volume,
            sustain_buckets=sustain_buckets,
        )
        if edge_metric_df.empty:
            return "No caller-callee trace edges found in the selected window"
        if not anomalies:
            return (
                "No anomalous caller-callee paths found. "
                f"Checked last {window_minutes} minutes with min_edge_volume={min_edge_volume}."
            )

        selected_edges = actions._select_trace_path_subgraph(
            anomalies,
            top_k_paths=top_k_paths,
            onset_slack_minutes=onset_slack_minutes,
            path_slack_minutes=max(onset_slack_minutes + 2, 5),
        )
        if not selected_edges:
            return "Trace anomalies were detected, but no connected path subgraph could be formed."

        out_dir = actions.save_dir or os.path.join(actions.work_dir, "static_metric_output")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "trace_anomalous_paths.png")
        actions._plot_trace_anomalous_path_figure(
            selected_edges,
            out_path,
            window_minutes=window_minutes,
            edge_metric_df=edge_metric_df,
        )

        summary_lines = [out_path, "", "Earliest anomalous edges:"]
        for item in selected_edges[: min(len(selected_edges), 8)]:
            onset = item["first_onset"].strftime("%Y-%m-%d %H:%M:%S")
            summary_lines.append(
                f"- {item['caller']} -> {item['callee']} | onset={onset} | mode={item['dominant_metric']}"
            )
        return "\n".join(summary_lines)


class MarketTracePathRenderer(BaseTracePathRenderer):
    """Renderer for OpenRCA Market datasets."""

    @staticmethod
    def _is_success_status(value: object) -> bool:
        text = str(value).strip().upper()
        return text in {"0", "OK", "200"}

    def _load_market_trace_window(self, actions, namespace: str, *, window_minutes: int | None) -> pd.DataFrame:
        raw = actions.static_app.fetch_traces_df(
            namespace,
            start_time=actions._query_start,
            end_time=actions._query_end,
        )
        if raw.empty:
            return raw
        df = raw.copy()
        if "timestamp" not in df.columns or "span_id" not in df.columns or "cmdb_id" not in df.columns:
            return pd.DataFrame()
        parent_col = "parent_span" if "parent_span" in df.columns else ("parent_id" if "parent_id" in df.columns else None)
        if parent_col is None:
            return pd.DataFrame()
        if "duration" not in df.columns:
            return pd.DataFrame()

        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
        df["duration"] = pd.to_numeric(df["duration"], errors="coerce")
        df = df.dropna(subset=["timestamp", "duration"]).copy()
        if df.empty:
            return df
        df["bucket"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.floor("1min")
        if window_minutes is not None and window_minutes > 0:
            end_time = df["bucket"].max()
            start_time = end_time - pd.Timedelta(minutes=max(window_minutes - 1, 0))
            df = df[df["bucket"] >= start_time].copy()

        df["span_id"] = df["span_id"].astype(str).str.strip()
        df[parent_col] = df[parent_col].fillna("").astype(str).str.strip()
        df["cmdb_id"] = df["cmdb_id"].astype(str).str.strip()
        df["success"] = df.get("status_code", "").map(self._is_success_status)
        df["operation_name"] = df.get("operation_name", "").fillna("").astype(str).str.strip()
        df["_parent_col"] = parent_col
        return df

    @staticmethod
    def _build_market_parent_edge_metric_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame()
        parent_col = str(df["_parent_col"].iloc[0]) if "_parent_col" in df.columns else "parent_span"
        parent_map = (
            df[["span_id", "cmdb_id", "duration", "success", "bucket", "operation_name"]]
            .drop_duplicates("span_id")
            .set_index("span_id")
        )
        children = df[df[parent_col] != ""].copy()
        children = children[children[parent_col].isin(parent_map.index)].copy()
        if children.empty:
            return pd.DataFrame()

        children["caller"] = children[parent_col].map(parent_map["cmdb_id"])
        children["callee"] = children["cmdb_id"]
        children["edge_duration"] = pd.to_numeric(children[parent_col].map(parent_map["duration"]), errors="coerce")
        children["edge_success"] = children[parent_col].map(parent_map["success"]).astype(bool)
        children["edge_bucket"] = children[parent_col].map(parent_map["bucket"])
        children["edge_operation"] = children[parent_col].map(parent_map["operation_name"]).fillna("")
        children = children[
            children["caller"].notna()
            & (children["caller"] != "")
            & (children["callee"] != "")
            & (children["caller"] != children["callee"])
            & children["edge_duration"].notna()
            & children["edge_bucket"].notna()
        ].copy()
        if children.empty:
            return pd.DataFrame()

        return (
            children.groupby(["caller", "callee", "edge_bucket"], as_index=False)
            .agg(
                latency_p50=("edge_duration", "median"),
                latency_p90=("edge_duration", lambda s: float(s.quantile(0.9))),
                latency_p99=("edge_duration", lambda s: float(s.quantile(0.99))),
                volume=("edge_duration", "size"),
                error_rate=("edge_success", lambda s: float((~s).mean() * 100.0)),
                operation_name=(
                    "edge_operation",
                    lambda s: str(s.mode().iloc[0]) if not s.mode().empty else str(s.iloc[0]) if len(s) else "",
                ),
            )
            .rename(columns={"edge_bucket": "bucket"})
            .sort_values(["caller", "callee", "bucket"])
        )

    @staticmethod
    def _select_market_normal_edges(edge_metric_df: pd.DataFrame, top_k_paths: int) -> list[dict]:
        if edge_metric_df.empty:
            return []
        score_df = (
            edge_metric_df.groupby(["caller", "callee"], as_index=False)
            .agg(
                max_volume=("volume", "max"),
                max_latency=("latency_p99", "max"),
                first_onset=("bucket", "min"),
            )
            .sort_values(["max_volume", "max_latency"], ascending=[False, False])
        )
        selected: list[dict] = []
        keep_n = max(int(top_k_paths), 1) * 2
        for _, row in score_df.head(keep_n).iterrows():
            caller = str(row["caller"])
            callee = str(row["callee"])
            series = edge_metric_df[(edge_metric_df["caller"] == caller) & (edge_metric_df["callee"] == callee)].copy()
            selected.append(
                {
                    "edge_id": f"{caller}->{callee}",
                    "caller": caller,
                    "callee": callee,
                    "first_onset": pd.to_datetime(row["first_onset"]),
                    "dominant_metric": "latency_edge",
                    "latency_info": None,
                    "error_info": None,
                    "gap_info": None,
                    "remote_info": None,
                    "series": series,
                    "score": float(row["max_volume"]),
                }
            )
        return selected

    @staticmethod
    def _detect_market_parent_edge_anomalies(
        edge_metric_df: pd.DataFrame,
        *,
        sustain_buckets: int,
        min_edge_volume: int,
    ) -> list[dict]:
        if edge_metric_df.empty:
            return []
        anomalies: list[dict] = []
        for (caller, callee), group in edge_metric_df.groupby(["caller", "callee"], sort=False):
            group = group.sort_values("bucket").reset_index(drop=True)
            if len(group) < max(6, sustain_buckets + 5):
                continue
            for idx in range(5, len(group) - sustain_buckets + 1):
                window = group.iloc[idx: idx + sustain_buckets]
                baseline_latency = float(group.iloc[idx - 5: idx]["latency_p99"].median())
                baseline_error = float(group.iloc[idx - 5: idx]["error_rate"].median())

                latency_thr = max(baseline_latency * 1.8, baseline_latency + 100.0, 80.0)
                error_thr = max(baseline_error * 3.0, baseline_error + 5.0, 1.0)

                latency_ok = bool((window["latency_p99"] >= latency_thr).all()) and bool((window["volume"] >= min_edge_volume).all())
                error_ok = bool((window["error_rate"] >= error_thr).all()) and bool((window["volume"] >= min_edge_volume).all())
                if not latency_ok and not error_ok:
                    continue

                dominant_metric = "error_edge" if error_ok and not latency_ok else "latency_edge"
                latency_info = None
                error_info = None
                if latency_ok:
                    latency_info = {
                        "baseline": float(baseline_latency),
                        "peak": float(window["latency_p99"].max()),
                        "threshold": float(latency_thr),
                        "duration_minutes": int(sustain_buckets),
                        "episode_count": 1,
                    }
                if error_ok:
                    error_info = {
                        "baseline": float(baseline_error),
                        "peak": float(window["error_rate"].max()),
                        "threshold": float(error_thr),
                        "duration_minutes": int(sustain_buckets),
                        "episode_count": 1,
                    }
                anomalies.append(
                    {
                        "edge_id": f"{caller}->{callee}",
                        "caller": caller,
                        "callee": callee,
                        "first_onset": pd.to_datetime(window.iloc[0]["bucket"]),
                        "dominant_metric": dominant_metric,
                        "latency_info": latency_info,
                        "error_info": error_info,
                        "gap_info": None,
                        "remote_info": None,
                        "series": group,
                        "score": float(window["latency_p99"].max()) + float(window["error_rate"].max()) * 100.0,
                    }
                )
                break
        return sorted(anomalies, key=lambda item: (item["first_onset"], -item["score"], item["edge_id"]))

    @staticmethod
    def _market_base_service(component: str) -> str:
        leaf = str(component or "").strip().split(".", 1)[-1]
        m = re.match(r"^([a-z][a-z0-9]*?)(?:2)?-\d+$", leaf)
        return m.group(1) if m else leaf

    @staticmethod
    def _pair_key(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def _infer_service_graph_depths(self, edge_metric_df: pd.DataFrame | None) -> dict[str, int]:
        if edge_metric_df is None or edge_metric_df.empty:
            return {}
        summary = (
            edge_metric_df
            .groupby(["caller", "callee"], as_index=False)
            .agg(score=("volume", "sum"))
            .sort_values(["caller", "score", "callee"], ascending=[True, False, True])
        )
        nodes = sorted(set(summary["caller"]) | set(summary["callee"]))
        if not nodes:
            return {}
        outgoing: dict[str, list[str]] = {node: [] for node in nodes}
        indegree: dict[str, int] = {node: 0 for node in nodes}
        for row in summary.itertuples(index=False):
            outgoing.setdefault(row.caller, [])
            if row.callee not in outgoing[row.caller]:
                outgoing[row.caller].append(row.callee)
            indegree[row.callee] = indegree.get(row.callee, 0) + 1
            indegree.setdefault(row.caller, 0)
        roots = [node for node in nodes if indegree.get(node, 0) == 0] or nodes[:]
        dist: dict[str, int] = {}
        queue: list[tuple[str, int]] = [(node, 0) for node in sorted(roots)]
        while queue:
            node, depth = queue.pop(0)
            prev = dist.get(node)
            if prev is not None and prev <= depth:
                continue
            dist[node] = depth
            for nxt in outgoing.get(node, []):
                queue.append((nxt, depth + 1))
        return dist

    def _canonicalize_plot_directions(
        self,
        selected_edges: list[dict],
        normal_edges: list[tuple[str, str]],
        edge_metric_df: pd.DataFrame | None = None,
    ) -> tuple[list[dict], list[tuple[str, str]]]:
        pair_weights: dict[tuple[str, str], dict[tuple[str, str], float]] = {}
        graph_depths = self._infer_service_graph_depths(edge_metric_df)
        if edge_metric_df is not None and not edge_metric_df.empty:
            summary = (
                edge_metric_df
                .groupby(["caller", "callee"], as_index=False)
                .agg(weight=("volume", "sum"))
            )
            for row in summary.itertuples(index=False):
                pair = self._pair_key(str(row.caller), str(row.callee))
                pair_weights.setdefault(pair, {})[(str(row.caller), str(row.callee))] = float(row.weight)

        selected_by_dir: dict[tuple[str, str], list[dict]] = {}
        for item in selected_edges:
            direction = (str(item["caller"]), str(item["callee"]))
            selected_by_dir.setdefault(direction, []).append(item)
            pair = self._pair_key(*direction)
            pair_weights.setdefault(pair, {}).setdefault(direction, float(item.get("score", 0.0)))

        normal_set = {(str(a), str(b)) for a, b in normal_edges}
        for direction in normal_set:
            pair = self._pair_key(*direction)
            pair_weights.setdefault(pair, {}).setdefault(direction, 0.0)

        keep_selected: list[dict] = []
        keep_normal: list[tuple[str, str]] = []
        handled_pairs: set[tuple[str, str]] = set()
        all_dirs = set(selected_by_dir) | normal_set
        for direction in sorted(all_dirs):
            pair = self._pair_key(*direction)
            if pair in handled_pairs:
                continue
            handled_pairs.add(pair)
            a, b = pair
            forward = (a, b)
            backward = (b, a)
            options = pair_weights.get(pair, {})
            if forward in options and backward in options:
                chosen = None
                da_graph = graph_depths.get(a)
                db_graph = graph_depths.get(b)
                if da_graph is not None and db_graph is not None and da_graph != db_graph:
                    chosen = (a, b) if da_graph < db_graph else (b, a)
                if chosen is None:
                    fw = float(options.get(forward, 0.0))
                    bw = float(options.get(backward, 0.0))
                    chosen = forward if fw >= bw else backward
            else:
                chosen = forward if forward in options else backward

            chosen_selected = selected_by_dir.get(chosen, [])
            if chosen_selected:
                best = sorted(
                    chosen_selected,
                    key=lambda item: (
                        str(item.get("dominant_metric", "")).strip().lower() == "normal",
                        -float(item.get("score", 0.0)),
                        item["edge_id"],
                    ),
                )[0]
                keep_selected.append(best)
            elif chosen in normal_set:
                keep_normal.append(chosen)

        keep_selected.sort(key=lambda item: (item["first_onset"], -item["score"], item["edge_id"]))
        keep_normal.sort()
        return keep_selected, keep_normal

    @staticmethod
    def _has_visible_edge_anomaly(item: dict) -> bool:
        return bool(
            item.get("latency_info")
            or item.get("error_info")
            or item.get("gap_info")
            or item.get("remote_info")
        )

    def _build_market_unrolled_display(
        self,
        selected_edges: list[dict],
        normal_edges: list[tuple[str, str]],
        *,
        max_depth: int = 5,
    ) -> tuple[dict[str, tuple[float, float]], list[tuple[str, str]], dict[str, str], dict[tuple[str, str], dict]]:
        edge_map: dict[tuple[str, str], dict] = {
            (str(item["caller"]), str(item["callee"])): item
            for item in selected_edges
        }
        outgoing: dict[str, list[str]] = {}
        nodes: set[str] = set()
        for caller, callee in list(edge_map.keys()) + list(normal_edges):
            caller = str(caller).strip()
            callee = str(callee).strip()
            if not caller or not callee or caller == callee:
                continue
            nodes.add(caller)
            nodes.add(callee)
            outgoing.setdefault(caller, [])
            if callee not in outgoing[caller]:
                outgoing[caller].append(callee)
        if not nodes:
            return {}, [], {}, {}

        service_depths = self._infer_service_graph_depths(
            pd.DataFrame([{"caller": c, "callee": d, "volume": 1} for c, d in list(edge_map.keys()) + list(normal_edges)])
        )
        indegree = {node: 0 for node in nodes}
        for caller, callees in outgoing.items():
            for callee in callees:
                indegree[callee] = indegree.get(callee, 0) + 1
        roots = sorted([node for node in nodes if indegree.get(node, 0) == 0]) or sorted(nodes)

        display_labels: dict[str, str] = {}
        display_edges: list[tuple[str, str]] = []
        display_anomalies: dict[tuple[str, str], dict] = {}
        layer_nodes: dict[int, list[str]] = {}
        queue: list[tuple[str, int]] = [(root, 0) for root in roots]
        seen_states: set[tuple[str, int]] = set(queue)
        seen_edges: set[tuple[str, str]] = set()

        while queue:
            node, depth = queue.pop(0)
            src_id = f"{node}@{depth}"
            if src_id not in display_labels:
                display_labels[src_id] = node
                layer_nodes.setdefault(depth, []).append(src_id)
            if depth >= max_depth - 1:
                continue

            ordered_callees = sorted(
                outgoing.get(node, []),
                key=lambda callee: (service_depths.get(callee, depth + 1), callee),
            )
            for callee in ordered_callees:
                dst_depth = max(depth + 1, service_depths.get(callee, depth + 1))
                dst_id = f"{callee}@{dst_depth}"
                if dst_id not in display_labels:
                    display_labels[dst_id] = callee
                    layer_nodes.setdefault(dst_depth, []).append(dst_id)
                edge_key = (src_id, dst_id)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    display_edges.append(edge_key)
                    anomaly_item = edge_map.get((node, callee))
                    if anomaly_item is not None:
                        display_anomalies[edge_key] = anomaly_item
                state = (callee, dst_depth)
                if state not in seen_states and dst_depth < max_depth:
                    seen_states.add(state)
                    queue.append(state)

        positions: dict[str, tuple[float, float]] = {}
        for depth, ids in sorted(layer_nodes.items()):
            ids = sorted(ids, key=lambda node_id: display_labels[node_id])
            count = len(ids)
            for row_idx, node_id in enumerate(ids):
                y = (count - 1) / 2.0 - row_idx
                positions[node_id] = (depth * 2.8, y * 1.5)
        return positions, display_edges, display_labels, display_anomalies

    @staticmethod
    def _edge_plot_color(dominant_metric: str) -> str:
        return {
            "latency_edge": "#ff7f0e",
            "error_edge": "#d62728",
            "gap_edge": "#8c564b",
        }.get(str(dominant_metric or ""), "#7f7f7f")

    def _edge_label(self, item: dict) -> str:
        onset = item["first_onset"].strftime("%H:%M")
        parts = [f"{item['caller']} -> {item['callee']}", onset]
        if item.get("latency_info"):
            d = int(float(item["latency_info"].get("duration_minutes", 0.0)))
            c = int(item["latency_info"].get("episode_count", 1))
            parts.append(f"L {item['latency_info']['peak']:.0f}ms D{d}m x{c}")
        if item.get("error_info"):
            d = int(float(item["error_info"].get("duration_minutes", 0.0)))
            c = int(item["error_info"].get("episode_count", 1))
            parts.append(f"E {item['error_info']['peak']:.0f}% D{d}m x{c}")
        if item.get("gap_info"):
            d = int(float(item["gap_info"].get("duration_minutes", 0.0)))
            c = int(item["gap_info"].get("episode_count", 1))
            parts.append(f"G {item['gap_info']['peak']:.0f}ms D{d}m x{c}")
        return "\n".join(parts)

    def _plot_market_paths(
        self,
        selected_edges: list[dict],
        out_path: str,
        *,
        window_minutes: int,
        edge_metric_df: pd.DataFrame,
    ) -> None:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D

        normal_edges: list[tuple[str, str]] = []
        visible_selected_edges = [item for item in selected_edges if self._has_visible_edge_anomaly(item)]
        anomalous_edge_ids = {item["edge_id"] for item in visible_selected_edges}
        nodes = {item["caller"] for item in selected_edges} | {item["callee"] for item in selected_edges}
        if edge_metric_df is not None and not edge_metric_df.empty:
            for (caller, callee), _ in edge_metric_df.groupby(["caller", "callee"], sort=True):
                eid = f"{caller}->{callee}"
                if eid not in anomalous_edge_ids and (caller in nodes or callee in nodes):
                    nodes.add(caller)
                    nodes.add(callee)
                    normal_edges.append((str(caller), str(callee)))

        visible_selected_edges, normal_edges = self._canonicalize_plot_directions(
            visible_selected_edges,
            normal_edges,
            edge_metric_df=edge_metric_df,
        )
        pos, display_edges, display_labels, display_anomalies = self._build_market_unrolled_display(
            visible_selected_edges,
            normal_edges,
        )
        fig = plt.figure(figsize=(16, 8), constrained_layout=True)
        ax = fig.add_subplot(111)
        ax.set_axis_off()

        for i, (src_id, dst_id) in enumerate(display_edges):
            if src_id not in pos or dst_id not in pos:
                continue
            if display_anomalies.get((src_id, dst_id)) is not None:
                continue
            sx, sy = pos[src_id]
            tx, ty = pos[dst_id]
            rad = 0.06 if i % 2 == 0 else -0.06
            ax.annotate("", xy=(tx - 0.3, ty), xytext=(sx + 0.3, sy),
                        arrowprops={"arrowstyle": "->", "lw": 1.2, "color": "#9E9E9E", "alpha": 0.6,
                                    "connectionstyle": f"arc3,rad={rad}"}, zorder=0)

        for i, ((src_id, dst_id), item) in enumerate(display_anomalies.items()):
            if src_id not in pos or dst_id not in pos:
                continue
            sx, sy = pos[src_id]
            tx, ty = pos[dst_id]
            color = self._edge_plot_color(str(item.get("dominant_metric", "")))
            rad = 0.08 if i % 2 == 0 else -0.08
            ax.annotate("", xy=(tx - 0.3, ty), xytext=(sx + 0.3, sy),
                        arrowprops={"arrowstyle": "->", "lw": 2.0, "color": color, "alpha": 0.9,
                                    "connectionstyle": f"arc3,rad={rad}"}, zorder=2)
            mx = (sx + tx) / 2.0
            my = (sy + ty) / 2.0 + (0.2 if i % 2 == 0 else -0.2)
            ax.text(mx, my, self._edge_label(item), fontsize=8, ha="center", va="center", color=color,
                    bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.85}, zorder=3)

        for node_id, (x, y) in pos.items():
            ax.text(x, y, display_labels.get(node_id, node_id), ha="center", va="center", fontsize=10,
                    bbox={"boxstyle": "round,pad=0.35", "fc": "#F7F8FA", "ec": "#4F5B67", "lw": 1.2}, zorder=4)

        legend = [Line2D([0], [0], color="#ff7f0e", lw=2, label="latency edge")]
        if any(str(e.get("dominant_metric")) == "error_edge" for e in visible_selected_edges):
            legend.append(Line2D([0], [0], color="#d62728", lw=2, label="error edge"))
        if normal_edges:
            legend.append(Line2D([0], [0], color="#9E9E9E", lw=1.5, label="normal"))
        ax.legend(handles=legend, loc="upper right", frameon=False, fontsize=8)
        ax.set_title(
            f"Anomalous caller-callee paths (strongest episode per edge, {window_minutes}-minute window)",
            fontsize=13,
            fontweight="bold",
            pad=12,
        )
        xs = [v[0] for v in pos.values()] or [0.0]
        ys = [v[1] for v in pos.values()] or [0.0]
        ax.set_xlim(min(xs) - 1.3, max(xs) + 1.3)
        ax.set_ylim(min(ys) - 1.3, max(ys) + 1.3)
        fig.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)

    def render(
        self,
        actions,
        namespace: str,
        *,
        window_minutes: int = 30,
        top_k_paths: int = 5,
        min_edge_volume: int = 20,
        onset_slack_minutes: int = 3,
        sustain_buckets: int = 2,
    ) -> str:
        _ = onset_slack_minutes  # kept for interface compatibility
        edge_metric_df, anomalies, _events = self.build_edge_events(
            actions,
            namespace,
            window_minutes=window_minutes,
            min_edge_volume=min_edge_volume,
            sustain_buckets=sustain_buckets,
        )
        if edge_metric_df.empty:
            return "No parent-span caller-callee edges found in the selected window"

        selected_edges = anomalies if anomalies else self._select_market_normal_edges(
            edge_metric_df, top_k_paths=top_k_paths
        )
        if not selected_edges:
            return "No caller-callee trace edges found to render"

        out_dir = actions.save_dir or os.path.join(actions.work_dir, "static_metric_output")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "trace_anomalous_paths.png")
        self._plot_market_paths(
            selected_edges,
            out_path,
            window_minutes=window_minutes,
            edge_metric_df=edge_metric_df,
        )

        if anomalies:
            summary_lines = [out_path, "", "Earliest anomalous edges (market parent-span):"]
            for item in selected_edges[: min(len(selected_edges), 8)]:
                onset = item["first_onset"].strftime("%Y-%m-%d %H:%M:%S")
                summary_lines.append(
                    f"- {item['caller']} -> {item['callee']} | onset={onset} | mode={item['dominant_metric']}"
                )
            return "\n".join(summary_lines)

        return "\n".join(
            [
                out_path,
                "",
                f"No anomaly detected (market parent-span criteria). Rendered representative normal paths from last {window_minutes} minutes.",
            ]
        )

    def build_edge_events(
        self,
        actions,
        namespace: str,
        *,
        window_minutes: int | None = 30,
        min_edge_volume: int = 20,
        sustain_buckets: int = 2,
    ) -> tuple[pd.DataFrame, list[dict], list[dict]]:
        df = self._load_market_trace_window(actions, namespace, window_minutes=window_minutes)
        if df.empty:
            return pd.DataFrame(), [], []
        edge_metric_df = self._build_market_parent_edge_metric_frame(df)
        if edge_metric_df.empty:
            return pd.DataFrame(), [], []
        anomalies = self._detect_market_parent_edge_anomalies(
            edge_metric_df,
            sustain_buckets=sustain_buckets,
            min_edge_volume=min_edge_volume,
        )
        return edge_metric_df, anomalies, self._events_from_anomalies(anomalies)


class TelecomTracePathRenderer(BaseTracePathRenderer):
    """Renderer for OpenRCA Telecom datasets."""


class BankTracePathRenderer(BaseTracePathRenderer):
    """Renderer for OpenRCA Bank datasets."""


class DefaultTracePathRenderer(BaseTracePathRenderer):
    """Fallback renderer when dataset-specific routing is unavailable."""


def _dataset_key_from_problem_id(problem_id: str) -> str:
    pid = (problem_id or "").strip().lower()
    if pid.startswith("openrca_market"):
        return "market"
    if pid.startswith("openrca_telecom"):
        return "telecom"
    if pid.startswith("openrca_bank"):
        return "bank"
    return "default"


def get_trace_path_renderer(problem_id: str):
    """Return dataset-specific renderer instance for this problem id."""
    dataset_key = _dataset_key_from_problem_id(problem_id)
    if dataset_key == "market":
        return MarketTracePathRenderer()
    if dataset_key == "telecom":
        return TelecomTracePathRenderer()
    if dataset_key == "bank":
        return BankTracePathRenderer()
    return DefaultTracePathRenderer()
