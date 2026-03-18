"""Manim animation of the RCA search tree.

Reads a tree.json saved by StagedRCAPipeline and produces an MP4
showing the 3-stage search process: Localize → Deep Dive → Expand.

Also provides LocalizationTimelineScene: x-axis = time, one row per
localization outlier candidate (component + reason).

Usage:
    manim -pql clients/tree_traversal/visualize_rca_tree.py RCATreeAnimation

    # With a specific tree file:
    TREE_JSON=results/.../tree.json manim -pql \
        clients/tree_traversal/visualize_rca_tree.py RCATreeAnimation

    # Localization timeline (time on x-axis):
    TREE_JSON=results/.../tree.json manim -pql \
        clients/tree_traversal/visualize_rca_tree.py LocalizationTimelineScene
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from manim import (
    DOWN, LEFT, RIGHT, UP, ORIGIN,
    Arrow, Axes, Circle, Create, Dot, FadeIn, FadeOut, Flash,
    GrowFromCenter, Line, ManimColor, Mobject,
    NumberLine, Scene, Text, VGroup, Write,
    config,
)
try:
    from manim import MovingCameraScene
except ImportError:
    MovingCameraScene = Scene  # older manim: no camera.frame, zoom skipped via hasattr

# ── Colours ───────────────────────────────────────────────────────────

STAGE_COLORS = {
    "root":      ManimColor("#888888"),
    "localize":  ManimColor("#4A90D9"),
    "deep_dive": ManimColor("#F5A623"),
    "expand":    ManimColor("#7ED321"),
}

STATUS_COLORS = {
    "candidate": ManimColor("#AAAAAA"),
    "confirmed": ManimColor("#4CAF50"),
    "pruned":    ManimColor("#E74C3C"),
    "active":    ManimColor("#888888"),
    "low_confidence": ManimColor("#E67E22"),  # orange: pruned due to low confidence
}
LOCALIZED_MATCH_FILL = ManimColor("#D7C6FF")
LOCALIZED_MATCH_EDGE = ManimColor("#7B61FF")

STAGE_LABELS = {
    "localize":  "Stage 1: Localize",
    "deep_dive": "Stage 2: Deep Dive",
    "expand":    "Stage 3: Expand",
}
RELATION_EDGE_COLORS = {
    "latency": ManimColor("#1f77b4"),
    "error_rate": ManimColor("#d62728"),
    "volume_drop": ManimColor("#ff7f0e"),
    "mixed": ManimColor("#9467bd"),
}


# ── Helper: build layout positions ───────────────────────────────────

def _layout_tree(nodes: list[dict], events: list[dict]) -> dict[str, tuple[float, float]]:
    """Compute (x, y) positions for each node using a simple layered layout.

    Layers:
      y=0   root
      y=-2  localize
      y=-4  deep_dive
      y=-6  expand
    """
    layer_y = {"root": 0, "localize": -2.5, "deep_dive": -5.0, "expand": -7.5}
    by_stage: dict[str, list[dict]] = {}
    for n in nodes:
        stage = n.get("stage", "root")
        by_stage.setdefault(stage, []).append(n)

    positions: dict[str, tuple[float, float]] = {}
    for stage, stage_nodes in by_stage.items():
        y = layer_y.get(stage, -9.0)
        count = len(stage_nodes)
        if count == 0:
            continue
        spacing = min(2.5, 12.0 / max(count, 1))
        start_x = -spacing * (count - 1) / 2
        for i, nd in enumerate(stage_nodes):
            nid = nd.get("id", f"n{i}")
            positions[nid] = (start_x + i * spacing, y)
    return positions


# ── Scene ─────────────────────────────────────────────────────────────

class RCATreeAnimation(MovingCameraScene):
    """Manim scene that replays the RCA search tree step by step.
    Uses MovingCameraScene so camera.frame exists for zoom (plain Scene's Camera has no .frame).
    """

    def construct(self):
        try:
            self._construct_tree()
        except Exception as e:
            import traceback
            err_text = f"Scene error:\n{e!s}\n\n{traceback.format_exc()}"[:1200]
            self.add(Text(err_text, font_size=12))
            self.wait(5)

    def _construct_tree(self):
        # Zoom out so the whole tree fits (MovingCameraScene has camera.frame; plain Scene does not).
        if hasattr(self.camera, "frame"):
            self.camera.frame.scale(1.3)

        tree_path_raw = os.environ.get(
            "TREE_JSON",
            "results/static_problems/openrca_market_cb1/kg-rca/tree.json",
        )
        tree_path = Path(tree_path_raw).resolve()
        if not tree_path.exists():
            self.add(Text(f"tree.json not found:\n{tree_path}", font_size=24))
            self.wait(3)
            return

        try:
            data = json.loads(tree_path.read_text())
        except Exception as e:
            self.add(Text(f"JSON error:\n{tree_path}\n{e!s}", font_size=20))
            self.wait(3)
            return

        nodes_data = data.get("nodes")
        events = data.get("events")
        if not nodes_data or not events:
            self.add(Text(
                f"Invalid tree.json: missing 'nodes' or 'events'\n{tree_path}",
                font_size=20,
            ))
            self.wait(3)
            return

        positions = _layout_tree(nodes_data, events)
        node_map = {n.get("id", ""): n for n in nodes_data}
        relation_edges = data.get("relation_edges", []) or []

        # Manim mobjects for each node
        mobjects: dict[str, VGroup] = {}
        arrows: dict[str, Arrow] = {}

        try:
            self._play_tree_animation(
                nodes_data, events, positions, node_map,
                mobjects, arrows, relation_edges,
            )
        except Exception as e:
            import traceback
            err_msg = f"Scene error:\n{e!s}\n\n{traceback.format_exc()[:800]}"
            self.add(Text(err_msg, font_size=14))
            self.wait(5)
            return

        self.wait(2)

    def _play_tree_animation(
        self,
        nodes_data: list[dict],
        events: list[dict],
        positions: dict[str, tuple[float, float]],
        node_map: dict[str, dict],
        mobjects: dict[str, VGroup],
        arrows: dict[str, Arrow],
        relation_edges: list[dict],
    ) -> None:
        """Run the tree replay (create/confirm/prune). Isolated so we can catch errors."""
        # Title
        title = Text("RCA Search Tree", font_size=36, color=ManimColor("#FFFFFF"))
        title.to_edge(UP, buff=0.3)
        self.play(Write(title), run_time=0.5)

        # Stage banner
        current_stage_label = Text("", font_size=28)
        current_stage_label.next_to(title, DOWN, buff=0.2)
        self.add(current_stage_label)

        prev_stage = None

        for event in events:
            nid = event.get("node_id", "")
            action = event.get("action", "")
            nd = node_map.get(nid, {})
            stage = nd.get("stage", "root")
            pos = positions.get(nid, (0, 0))

            # Show stage transition banner
            if stage != prev_stage and stage in STAGE_LABELS:
                new_label = Text(
                    STAGE_LABELS[stage],
                    font_size=28,
                    color=STAGE_COLORS.get(stage, ManimColor("#FFFFFF")),
                )
                new_label.move_to(current_stage_label.get_center())
                self.play(
                    FadeOut(current_stage_label, run_time=0.2),
                    FadeIn(new_label, run_time=0.3),
                )
                current_stage_label = new_label
                prev_stage = stage

            if action == "create":
                mob = self._make_node(nd, pos)
                mobjects[nid] = mob
                self.play(GrowFromCenter(mob), run_time=0.4)

                # Arrow from parent
                pid = nd.get("parent_id")
                if pid and pid in mobjects:
                    parent_mob = mobjects[pid]
                    arrow = Arrow(
                        parent_mob.get_bottom(),
                        mob.get_top(),
                        buff=0.1,
                        stroke_width=1.5,
                        color=STAGE_COLORS.get(stage, ManimColor("#AAAAAA")),
                    )
                    arrows[nid] = arrow
                    self.play(Create(arrow), run_time=0.3)

            elif action == "confirm":
                if nid in mobjects:
                    circle = mobjects[nid][0]
                    self.play(
                        circle.animate.set_fill(
                            STATUS_COLORS["confirmed"], opacity=0.8
                        ),
                        run_time=0.3,
                    )
                    self.play(Flash(mobjects[nid], color=STATUS_COLORS["confirmed"]))

            elif action == "prune":
                if nid in mobjects:
                    circle = mobjects[nid][0]
                    evidence = nd.get("evidence") or ""
                    reason = event.get("reason") or ""
                    is_low_conf = "Low confidence" in evidence or "Low confidence" in reason
                    fill_color = (
                        STATUS_COLORS["low_confidence"]
                        if is_low_conf
                        else STATUS_COLORS["pruned"]
                    )
                    self.play(
                        circle.animate.set_fill(fill_color, opacity=0.6),
                        run_time=0.3,
                    )
                    # Dim the arrow too
                    if nid in arrows:
                        self.play(
                            arrows[nid].animate.set_opacity(0.2),
                            run_time=0.2,
                        )

        overlay_mobjects = self._build_relation_overlay_mobjects(
            relation_edges,
            node_map,
            mobjects,
        )
        for mob in overlay_mobjects:
            self.play(Create(mob), run_time=0.2)

        # Highlight the best node at the end
        best_id = self._find_best(nodes_data)
        if best_id and best_id in mobjects:
            self.wait(0.5)
            best_nd = node_map[best_id]
            summary = Text(
                f"Root Cause: {best_nd.get('component', '?')} — "
                f"{best_nd.get('reason', '?')}",
                font_size=24,
                color=STATUS_COLORS["confirmed"],
            )
            summary.to_edge(DOWN, buff=0.3)
            self.play(Write(summary), run_time=0.8)
            self.play(Flash(mobjects[best_id], color=ManimColor("#FFD700")))

        self.wait(2)

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _relation_edge_color(edge: dict) -> ManimColor:
        anomaly = str(edge.get("anomaly_type") or "").strip()
        return RELATION_EDGE_COLORS.get(anomaly, ManimColor("#7f7f7f"))

    @staticmethod
    def _relation_edge_label(edge: dict) -> str:
        relation_type = str(edge.get("relation_type") or "").strip()
        anomaly = str(edge.get("anomaly_type") or "").strip()
        time_str = str(edge.get("time") or "").strip()
        parts: list[str] = []
        if relation_type:
            parts.append(relation_type.replace("call_", ""))
        if anomaly:
            parts.append(anomaly.replace("_", "-"))
        if time_str:
            parts.append(time_str.split()[1] if " " in time_str else time_str)
        return " / ".join(parts[:3])

    def _build_relation_overlay_mobjects(
        self,
        relation_edges: list[dict],
        node_map: dict[str, dict],
        mobjects: dict[str, VGroup],
    ) -> list[Mobject]:
        """Build non-tree directional relation overlays shown after replay."""
        out: list[Mobject] = []
        for edge in relation_edges:
            if not isinstance(edge, dict):
                continue
            if str(edge.get("relation_family") or "") == "deployment_contains":
                continue
            src = str(edge.get("src_node_id") or "").strip()
            dst = str(edge.get("dst_node_id") or "").strip()
            if src not in mobjects or dst not in mobjects:
                continue
            if (node_map.get(src) or {}).get("status") == "pruned":
                continue
            if (node_map.get(dst) or {}).get("status") == "pruned":
                continue
            src_mob = mobjects[src]
            dst_mob = mobjects[dst]
            color = self._relation_edge_color(edge)
            arrow = Arrow(
                src_mob.get_center(),
                dst_mob.get_center(),
                buff=0.32,
                stroke_width=2.0,
                color=color,
            )
            out.append(arrow)
            label_text = self._relation_edge_label(edge)
            if label_text:
                label = Text(label_text, font_size=10, color=color)
                label.move_to((src_mob.get_center() + dst_mob.get_center()) / 2)
                out.append(label)
        return out

    @staticmethod
    def _make_node(nd: dict, pos: tuple[float, float]) -> VGroup:
        stage = nd.get("stage", "root")
        status = nd.get("status", "candidate")
        component = nd.get("component", "?")
        reason = nd.get("reason", "")
        if stage in ("deep_dive", "expand"):
            reason = (nd.get("root_cause_reason_class") or reason) or ""
        confidence = nd.get("confidence")
        localized_match = bool(nd.get("localized_match", False))
        localized_time = (nd.get("localized_time") or "").strip()
        localized_severity = float(nd.get("localized_severity", 0.0) or 0.0)
        relation = (nd.get("relation") or "").strip()

        radius = 0.3 if stage == "root" else 0.25
        circle_color = (
            LOCALIZED_MATCH_EDGE
            if stage == "expand" and localized_match
            else STAGE_COLORS.get(stage, ManimColor("#AAAAAA"))
        )
        fill_color = (
            LOCALIZED_MATCH_FILL
            if stage == "expand" and localized_match
            else STATUS_COLORS.get(status, ManimColor("#AAAAAA"))
        )
        circle = Circle(
            radius=radius,
            color=circle_color,
            fill_color=fill_color,
            fill_opacity=0.5,
            stroke_width=2,
        )
        circle.move_to([pos[0], pos[1], 0])

        label_text = component[:16]
        if stage == "localize":
            kpi_val = nd.get("kpi", None)
            kpi_list: list[str] = []
            if isinstance(kpi_val, str):
                kpi_list = [k.strip() for k in kpi_val.split(",") if k.strip()]
            elif isinstance(kpi_val, (list, tuple)):
                kpi_list = [str(k).strip() for k in kpi_val if str(k).strip()]
            if kpi_list:
                label_text += "\n" + "\n".join(kpi_list[:3])
        elif reason:
            label_text += f"\n{reason[:20]}"
        if confidence is not None:
            pct = int(round(confidence * 100)) if confidence <= 1 else int(round(confidence))
            label_text += f"\n{pct}%"
        if stage == "expand" and localized_match:
            badge = "L1"
            if localized_time:
                t_loc = localized_time.split()[1] if " " in localized_time else localized_time
                badge = f"{badge}@{t_loc}"
            if localized_severity > 0:
                badge = f"{badge} sev{int(round(localized_severity))}"
            label_text += f"\n{badge}"
        if stage == "expand" and relation:
            label_text += f"\n{relation[:24]}"
        label = Text(label_text, font_size=11)
        label.next_to(circle, DOWN, buff=0.05)

        return VGroup(circle, label)

    @staticmethod
    def _find_best(nodes_data: list[dict]) -> str | None:
        """Best confirmed node, or if none, highest-confidence deep_dive/expand node."""
        confirmed = [n for n in nodes_data if n.get("status") == "confirmed"]
        if confirmed:
            confirmed.sort(key=lambda n: -n.get("confidence", 0))
            return confirmed[0]["id"]
        candidates = [
            n for n in nodes_data
            if n.get("confidence", 0) > 0 and n.get("stage") in ("deep_dive", "expand")
        ]
        if not candidates:
            return None
        best = max(
            candidates,
            key=lambda n: (n.get("confidence", 0), 1 if n.get("stage") == "deep_dive" else 0),
        )
        return best["id"]


# ── Localization timeline (x = time) ───────────────────────────────────

def _parse_time(time_str: str | None) -> float | None:
    """Parse 'YYYY-MM-DD HH:MM:SS' to minutes since epoch (for scaling)."""
    if not time_str or not time_str.strip():
        return None
    try:
        dt = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
        return dt.timestamp() / 60.0
    except ValueError:
        return None


def _localization_timeline_from_tree(tree_path: str | Path) -> list[dict]:
    """Extract localization-stage nodes with time for timeline plot.

    Returns list of {time_min, component, reason, node_id, severity_hint}.
    """
    data = json.loads(Path(tree_path).read_text())
    nodes = [n for n in data["nodes"] if n.get("stage") == "localize"]
    result = []
    for n in nodes:
        t = _parse_time(n.get("time"))
        if t is None:
            continue
        kpi_val = n.get("kpi", None)
        if isinstance(kpi_val, str):
            kpi_str = kpi_val
        elif isinstance(kpi_val, (list, tuple)):
            kpi_str = ", ".join(str(k).strip() for k in kpi_val if str(k).strip())
        else:
            kpi_str = ""
        result.append({
            "time_min": t,
            "component": n.get("component", "?"),
            "reason": n.get("reason") or n.get("evidence", "")[:30],
            "node_id": n.get("id", ""),
            "kpi": kpi_str,
        })
    result.sort(key=lambda x: x["time_min"])
    return result


class LocalizationTimelineScene(Scene):
    """Manim scene: localization outlier candidates with time on the x-axis.

    Reads tree.json (or LOCALIZATION_JSON), uses only stage=localize nodes.
    X-axis = time; each candidate is a labeled point/marker on a row (by component).
    """

    def construct(self):
        tree_path = os.environ.get(
            "TREE_JSON",
            os.environ.get("LOCALIZATION_JSON", "tree.json"),
        )
        if not Path(tree_path).exists():
            self.add(Text(
                f"tree.json not found:\n{tree_path}\n"
                "Set TREE_JSON or LOCALIZATION_JSON.",
                font_size=24,
            ))
            self.wait(3)
            return

        items = _localization_timeline_from_tree(tree_path)
        if not items:
            self.add(Text(
                "No localization candidates with timestamps in tree.",
                font_size=28,
            ))
            self.wait(3)
            return

        title = Text(
            "Localization: anomaly candidates over time",
            font_size=36,
            color=ManimColor("#FFFFFF"),
        )
        title.to_edge(UP, buff=0.3)
        self.play(Write(title), run_time=0.5)

        # Time range for x-axis
        t_min = min(x["time_min"] for x in items)
        t_max = max(x["time_min"] for x in items)
        t_span = max((t_max - t_min) / 60.0, 1.0)  # at least 1 minute span for scale

        # Map time to x in [-5, 5] (Manim units)
        def time_to_x(t: float) -> float:
            return -5.0 + 10.0 * (t - t_min) / t_span if t_span else 0

        # Unique (component, reason) for y-positions
        seen = set()
        rows = []
        for x in items:
            key = (x["component"], x["reason"][:20] if x["reason"] else "")
            if key not in seen:
                seen.add(key)
                rows.append((x["component"], x["reason"][:24] if x["reason"] else ""))

        n_rows = max(len(rows), 1)
        y_bottom = -2.5
        y_top = 2.0
        row_height = (y_top - y_bottom) / max(n_rows, 1)

        # Axes: horizontal time line at y=0
        axis_label = Text("Time →", font_size=20)
        axis_label.next_to(ORIGIN, RIGHT, buff=0.2)
        axis_label.shift(UP * (y_bottom - 0.5))
        self.add(axis_label)

        time_axis = Line(
            start=[-5.2, y_bottom - 0.5, 0],
            end=[5.2, y_bottom - 0.5, 0],
            color=ManimColor("#888888"),
        )
        self.play(Create(time_axis), run_time=0.3)

        # Row labels (component) on the left
        labels_vg = VGroup()
        for i, (comp, reason) in enumerate(rows):
            y_pos = y_top - (i + 0.5) * row_height
            lab = Text(
                f"{comp[:14]}\n{reason}" if reason else comp[:18],
                font_size=14,
            )
            lab.move_to([-5.8, y_pos, 0])
            lab.align_to(ORIGIN, RIGHT)
            labels_vg.add(lab)
        self.play(FadeIn(labels_vg), run_time=0.4)

        # Points for each candidate at (time, row)
        comp_reason_to_row = {}
        for i, (comp, reason) in enumerate(rows):
            comp_reason_to_row[(comp, reason)] = i

        dots_vg = VGroup()
        for x in items:
            tx = x["time_min"]
            comp = x["component"]
            reason = (x["reason"][:24] if x["reason"] else "")
            row_idx = comp_reason_to_row.get((comp, reason), 0)
            y_pos = y_top - (row_idx + 0.5) * row_height
            mx = time_to_x(tx)
            dot = Dot(
                point=[mx, y_pos, 0],
                color=STAGE_COLORS["localize"],
                radius=0.12,
            )
            dots_vg.add(dot)
        self.play(GrowFromCenter(dots_vg), run_time=0.6)

        # Optional: show first time and last time on axis
        t0_str = datetime.fromtimestamp(t_min * 60).strftime("%H:%M")
        t1_str = datetime.fromtimestamp(t_max * 60).strftime("%H:%M")
        t0_text = Text(t0_str, font_size=16)
        t0_text.move_to([-5.2, y_bottom - 1.0, 0])
        t1_text = Text(t1_str, font_size=16)
        t1_text.move_to([5.2, y_bottom - 1.0, 0])
        self.add(t0_text, t1_text)

        self.wait(2)
