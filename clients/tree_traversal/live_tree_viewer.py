"""실시간(on-process) Localization 타임라인 및 Search Tree 뷰어 (matplotlib).

- mode=timeline: x축 시간, y축 컴포넌트 (localize 노드만)
- mode=tree: RCA 검색 트리 전체 (root → localize → deep_dive → expand) 실시간 시각화
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from clients.tree_traversal.rca_search_tree import SearchTree

logger = logging.getLogger("live_tree_viewer")

# Tree view colors (align with visualize_rca_tree.py)
STAGE_COLORS = {
    "root": "#888888",
    "localize": "#4A90D9",
    "deep_dive": "#F5A623",
    "expand": "#7ED321",
}
STATUS_COLORS = {
    "candidate": "#AAAAAA",
    "confirmed": "#4CAF50",
    "pruned": "#E74C3C",
    "active": "#888888",
    "low_confidence": "#E67E22",
}
LOCALIZED_MATCH_FILL = "#D7C6FF"
LOCALIZED_MATCH_EDGE = "#7B61FF"


def _has_matplotlib() -> bool:
    try:
        import matplotlib  # noqa: F401
        import matplotlib.pyplot as plt  # noqa: F401
        return True
    except Exception:
        return False


def _parse_time_epoch(time_str: str | None) -> float | None:
    """문자열 시간을 '하루 중 몇 분'으로 파싱 (날짜는 무시)."""
    if not time_str:
        return None
    s = time_str.strip()
    s = re.sub(r"\s*(UTC|Z)$", "", s, flags=re.I)
    # Strip "approx", "~", "around" so we can parse the time part
    for prefix in ("approx ", "approximately ", "~", "around ", "at "):
        if s.lower().startswith(prefix):
            s = s[len(prefix):].strip()
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%H:%M:%S",
        "%H:%M",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            # 날짜는 버리고 시각(HH:MM:SS)만 분 단위로 사용
            return dt.hour * 60.0 + dt.minute + dt.second / 60.0
        except ValueError:
            continue
    # Regex fallback: 1–2 digit hour, e.g. 2:22:00 or 02:22
    m = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*", s)
    if m:
        try:
            h, mi = int(m.group(1)), int(m.group(2))
            sec = int(m.group(3)) if m.group(3) else 0
            if 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= sec <= 59:
                return h * 60.0 + mi + sec / 60.0
        except (ValueError, IndexError):
            pass
    return None


class LiveTreeViewer:
    """파이프라인과 같은 프로세스에서 트리/타임라인을 실시간으로 그리는 뷰어."""

    def __init__(
        self,
        title: str = "RCA Search (Live)",
        mode: str = "timeline",
        save_path: str | None = None,
        window_start_min: float | None = None,
        window_end_min: float | None = None,
    ):
        """
        Args:
            title: 창 제목
            mode: 'timeline' (localize 타임라인) 또는 'tree' (검색 트리 전체)
            save_path: PNG를 덮어쓰며 저장할 경로
            window_start_min: 쿼리 시작 시각 (epoch 초 / 60.0) — timeline 모드에서만 사용
            window_end_min: 쿼리 종료 시각 — timeline 모드에서만 사용
        """
        self.title = title
        self.mode = mode
        self.save_path = save_path
        self.window_start_min = window_start_min
        self.window_end_min = window_end_min
        self._fig = None
        self._ax = None

    def _ensure_backend(self) -> bool:
        if not _has_matplotlib():
            logger.warning("matplotlib 없음. pip install matplotlib 후 --live-view 사용.")
            return False
        return True

    def update(self, tree: "SearchTree") -> None:
        """트리 상태를 반영해 그래프를 다시 그립니다 (on-process에서 호출)."""
        if not self._ensure_backend():
            return
        import matplotlib.pyplot as plt

        if self.mode == "timeline":
            self._draw_timeline(tree)
        elif self.mode == "tree":
            self._draw_tree(tree)

        if self._fig is None:
            return
        plt.figure(self._fig.number)
        plt.suptitle(self.title, fontsize=10)
        plt.draw()

        # PNG로 스냅샷 저장
        if self.save_path:
            try:
                self._fig.savefig(self.save_path, dpi=150, bbox_inches="tight")
            except Exception as e:
                logger.warning("live view 저장 실패: %s", e)

    def close(self) -> None:
        """Close the figure and release resources."""
        if self._fig is None:
            return
        try:
            import matplotlib.pyplot as plt
            plt.close(self._fig)
        except Exception:
            pass
        self._fig = None
        self._ax = None

    # ------------------------------------------------------------------ #
    # 타임라인 (Localization candidates)                                 #
    # ------------------------------------------------------------------ #

    def _draw_timeline(self, tree: "SearchTree") -> None:
        import matplotlib.pyplot as plt

        # 1) localization 노드 수집
        items: list[dict] = []
        for n in tree.nodes.values():
            if n.stage != "localize" or not n.time:
                continue
            minute = _parse_time_epoch(n.time)
            if minute is None:
                continue
            items.append(
                {
                    "minute": minute,
                    "component": n.component or "?",
                    "label": (n.kpi or n.reason or n.evidence or "")[:40],
                    "status": n.status,
                    "confidence": getattr(n, "confidence", None),
                    "evidence": getattr(n, "evidence", "") or "",
                    "severity": getattr(n, "severity", 0.0) or 0.0,
                }
            )

        if not items:
            return

        # 2) 쿼리 윈도우: start~end (분 단위, 날짜는 무시)
        anomaly_mins = [it["minute"] for it in items]
        if self.window_start_min is not None and self.window_end_min is not None:
            start_min = float(self.window_start_min)
            end_min = float(self.window_end_min)
        else:
            start_min = min(anomaly_mins)
            end_min = max(anomaly_mins)

        # x좌표: 윈도우 시작 시각을 0으로 두고, 분 단위 offset
        xs = [(m - start_min) for m in anomaly_mins]
        span_min = max(end_min - start_min, 1.0)

        # 3) y축: 컴포넌트별 한 줄
        components: list[str] = []
        for it in items:
            if it["component"] not in components:
                components.append(it["component"])

        if self._fig is None:
            height = max(4, len(components) * 0.9)
            self._fig, self._ax = plt.subplots(1, 1, figsize=(10, height))
        self._ax.clear()

        comp_to_y = {c: i for i, c in enumerate(components)}

        # 4) 같은 노드·비슷한 시간대(같은 분) 안에 여러 이유가 있으면
        #    세로로 약간씩 벌려서 겹치지 않게 클러스터링
        bucket_sizes: dict[tuple[str, int], int] = defaultdict(int)
        for it, m in zip(items, anomaly_mins):
            key = (it["component"], int(round(m)))
            bucket_sizes[key] += 1

        bucket_seen: dict[tuple[str, int], int] = defaultdict(int)
        ys: list[float] = []
        colors: list[str] = []
        for it, m in zip(items, anomaly_mins):
            comp = it["component"]
            base_y = comp_to_y.get(comp, 0)
            key = (comp, int(round(m)))
            idx = bucket_seen[key]
            bucket_seen[key] += 1
            n = bucket_sizes[key]
            if n <= 1:
                offset = 0.0
            else:
                spread = 0.5
                step = spread / max(n - 1, 1)
                offset = -spread / 2 + idx * step

            ys.append(base_y + offset)
            status = it["status"]
            evidence = (it.get("evidence") or "").strip()
            is_low_conf_prune = (
                status == "pruned" and "Low confidence" in evidence
            )
            if status == "confirmed":
                colors.append("green")
            elif is_low_conf_prune:
                colors.append("orange")  # low_confidence (주황)
            elif status == "pruned":
                colors.append("red")
            else:
                colors.append("steelblue")

        self._ax.scatter(xs, ys, c=colors, s=80, alpha=0.8, zorder=2)
        self._ax.set_yticks(range(len(components)))
        self._ax.set_yticklabels(components, fontsize=8)

        # 5) label: kpi or root cause reason (+ confidence or severity if present)
        for idx, (x, y, it) in enumerate(zip(xs, ys, items)):
            txt = (it["label"] or "")
            conf = it.get("confidence")
            sev = it.get("severity", 0) or 0
            if conf is not None and conf > 0:
                pct = int(round(conf * 100)) if conf <= 1 else int(round(conf))
                txt = f"{txt} ({pct}%)" if txt else f"{pct}%"
            elif sev > 0:
                txt = f"{txt} (sev {int(sev)})" if txt else f"sev {int(sev)}"
            if not txt:
                continue
            # 좌우로 번갈아가며 배치해서 겹침을 줄인다.
            dx = 12 if (idx % 2 == 0) else -12
            ha = "left" if dx > 0 else "right"
            self._ax.annotate(
                txt,
                (x, y),
                textcoords="offset points",
                xytext=(dx, 4),
                ha=ha,
                fontsize=8,
                alpha=0.9,
                bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.6),
            )

        # 6) trace edges between components (caller → callee)
        comp_pos = {}
        for x, y, it in zip(xs, ys, items):
            comp = it["component"]
            if comp not in comp_pos:
                comp_pos[comp] = (x, y)
        trace_edges = getattr(tree, "trace_edges", []) or []
        for edge in trace_edges:
            src = edge.get("from")
            dst = edge.get("to")
            if not src or not dst:
                continue
            if src in comp_pos and dst in comp_pos:
                x1, y1 = comp_pos[src]
                x2, y2 = comp_pos[dst]
                self._ax.plot(
                    [x1, x2],
                    [y1, y2],
                    color="gray",
                    alpha=0.3,
                    linewidth=0.7,
                    zorder=1,
                )

        # 7) x축 tick: 쿼리 윈도우 start~end를 5분 간격으로
        # start_min은 epoch/60이므로, 다시 초 단위로 바꿔서 라벨 생성
        start_sec = start_min * 60.0
        start_dt = datetime.utcfromtimestamp(start_sec)
        tick_positions: list[float] = []
        tick_labels: list[str] = []
        offset = 0.0
        while offset <= span_min + 1e-6:
            tick_positions.append(offset)
            tick_labels.append((start_dt + timedelta(minutes=offset)).strftime("%H:%M"))
            offset += 5.0

        self._ax.set_xticks(tick_positions)
        self._ax.set_xticklabels(tick_labels, fontsize=8)
        self._ax.set_xlabel("Time (HH:MM)")
        self._ax.set_title("Localization: anomaly candidates (x=time)")
        self._ax.grid(True, alpha=0.3)
        self._fig.tight_layout()

    # ------------------------------------------------------------------ #
    # Search Tree (root → localize → deep_dive → expand)                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _tree_layout(nodes: dict) -> dict[str, tuple[float, float]]:
        """Planar-ish tree layout that minimizes edge crossings.

        Strategy:
        - Use parent/child structure from the search tree (not just stage buckets).
        - Recursively place children first (left-to-right), then center parent
          above the span of its children (classic tidy-tree layout).
        - Vertical position is derived from logical stage:
            root → y=0, localize → -2.5, deep_dive → -5, expand → -7.5.
        """
        if not nodes:
            return {}

        # Map stage → vertical depth
        stage_depth = {"root": 0, "localize": 1, "deep_dive": 2, "expand": 3}

        # Build parent → children adjacency and find roots.
        children_by_parent: dict[str, list] = defaultdict(list)
        roots: list = []
        for n in nodes.values():
            pid = getattr(n, "parent_id", None)
            if pid:
                children_by_parent[pid].append(n)
            else:
                roots.append(n)

        # Stable ordering for roots/children to keep layout deterministic.
        def _sort_key(node) -> tuple:
            st = getattr(node, "stage", "")
            depth = stage_depth.get(st, 99)
            return (
                depth,
                getattr(node, "component", "") or "",
                getattr(node, "kpi", "") or getattr(node, "root_cause_reason_class", "") or getattr(node, "reason", "") or "",
                getattr(node, "id", ""),
            )

        roots.sort(key=_sort_key)
        for pid, lst in children_by_parent.items():
            lst.sort(key=_sort_key)

        positions: dict[str, tuple[float, float]] = {}
        next_x = [0.0]  # mutable so inner function can update
        h_spacing = 1.8
        v_step = 2.5

        def _place(node) -> None:
            """Recursive tidy-tree placement for a single subtree."""
            nid = getattr(node, "id", None)
            if not nid:
                return
            children = children_by_parent.get(nid, [])
            # Place all children first.
            for ch in children:
                _place(ch)
            if not children:
                # Leaf: assign next free slot on x-axis.
                x = next_x[0] * h_spacing
                next_x[0] += 1.0
            else:
                # Internal node: center above children span.
                xs = [positions[getattr(ch, "id")] [0] for ch in children if getattr(ch, "id", None) in positions]
                if xs:
                    x = sum(xs) / len(xs)
                else:
                    x = next_x[0] * h_spacing
                    next_x[0] += 1.0
            # Base depth from logical stage.
            stage = getattr(node, "stage", "")
            depth = stage_depth.get(stage, 4)
            # If this is a second-hop expand (parent is also expand), push it one level lower
            # so 2-hop expand nodes are drawn on a separate 4th visual level.
            if stage == "expand":
                pid = getattr(node, "parent_id", None)
                parent = nodes.get(pid) if pid else None
                if parent is not None and getattr(parent, "stage", "") == "expand":
                    depth += 1
            y = -v_step * depth
            positions[nid] = (x, y)

        # Layout each root subtree in order.
        for r in roots:
            _place(r)

        # Center the whole tree around x=0 for nicer framing.
        if positions:
            min_x = min(p[0] for p in positions.values())
            max_x = max(p[0] for p in positions.values())
            shift = (min_x + max_x) / 2.0
            if abs(shift) > 1e-6:
                for nid, (x, y) in list(positions.items()):
                    positions[nid] = (x - shift, y)

        return positions

    def _draw_tree(self, tree: "SearchTree") -> None:
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        nodes = tree.nodes
        if not nodes:
            return
        positions = self._tree_layout(nodes)

        if self._fig is None:
            self._fig, self._ax = plt.subplots(1, 1, figsize=(12, 8))
        self._ax.clear()
        self._ax.set_aspect("equal")
        self._ax.axis("off")

        # Edges (parent → child)
        for nid, node in nodes.items():
            pid = node.parent_id
            if not pid or pid not in positions or nid not in positions:
                continue
            x1, y1 = positions[pid]
            x2, y2 = positions[nid]
            self._ax.plot([x1, x2], [y1, y2], color="gray", alpha=0.4, linewidth=1.0, zorder=0)

        # Relation edges (non-tree overlays: trace_call, deployment, shared_resource)
        # First, compute the highest (shallowest) y-position for each component so
        # that we can hide overlays involving duplicate components that already
        # appeared at an upper level.
        comp_top_y: dict[str, float] = {}
        for nid, (x_pos, y_pos) in positions.items():
            node = nodes.get(nid)
            if not node:
                continue
            comp = (getattr(node, "component", "") or "").strip()
            if not comp:
                continue
            if comp not in comp_top_y or y_pos > comp_top_y[comp]:
                comp_top_y[comp] = y_pos

        relation_edges = getattr(tree, "relation_edges", []) or []
        for edge in relation_edges:
            if not isinstance(edge, dict):
                continue
            family = (edge.get("relation_family") or "").strip()
            if family == "deployment_contains":
                # Deployment containment is rendered via containment boxes, not arrows.
                continue
            src_id = (edge.get("src_node_id") or "").strip()
            dst_id = (edge.get("dst_node_id") or "").strip()
            if src_id not in positions or dst_id not in positions:
                continue

            # Skip overlays where either endpoint is a lower-level duplicate of a
            # component that already appeared at an upper level (within ±5 minutes
            # of the earliest occurrence). This avoids visually confusing repeated
            # self-relations across levels for the same component.
            def _is_lower_duplicate(nid: str) -> bool:
                node = nodes.get(nid)
                if not node:
                    return False
                comp = (getattr(node, "component", "") or "").strip()
                if not comp or comp not in comp_top_y:
                    return False
                x_n, y_n = positions.get(nid, (None, None))
                if y_n is None:
                    return False
                top_y = comp_top_y[comp]
                if y_n >= top_y - 1e-6:
                    return False
                # If time is close to a top-level instance, treat as duplicate.
                if not node.time:
                    return True
                base_t = None
                for nid2, (x2, y2) in positions.items():
                    n2 = nodes.get(nid2)
                    if (
                        n2
                        and (n2.component or "").strip() == comp
                        and abs(y2 - top_y) < 1e-6
                    ):
                        base_t = _parse_time_epoch(str(n2.time)) if n2.time else None
                        break
                cur_t = _parse_time_epoch(str(node.time))
                if base_t is not None and cur_t is not None:
                    if abs(cur_t - base_t) <= 5.0:
                        return True
                return False

            if _is_lower_duplicate(src_id) or _is_lower_duplicate(dst_id):
                continue

            x1, y1 = positions[src_id]
            x2, y2 = positions[dst_id]
            rel_type = (edge.get("relation_type") or "").strip()
            anomaly = (edge.get("anomaly_type") or "").strip()
            time_str = (edge.get("time") or "").strip()

            # Style by relation family / anomaly type
            if family == "trace_call":
                # Thick red curved arrow for trace caller→callee relations
                color = "#E74C3C"
                lw = 2.4
                alpha = 0.9
                # Slight curvature based on direction to avoid overlap
                rad = 0.25 if rel_type == "call_downstream" else -0.25
                self._ax.annotate(
                    "",
                    xy=(x2, y2),
                    xytext=(x1, y1),
                    arrowprops=dict(
                        arrowstyle="->",
                        color=color,
                        lw=lw,
                        alpha=alpha,
                        shrinkA=10,
                        shrinkB=10,
                        connectionstyle=f"arc3,rad={rad}",
                    ),
                    zorder=3,
                )
                # Compact label near the middle of the curve
                mid_x = (x1 + x2) / 2.0
                mid_y = (y1 + y2) / 2.0 + (0.25 if rad > 0 else -0.25)
                parts = []
                if rel_type:
                    parts.append(rel_type.replace("call_", ""))
                if anomaly:
                    parts.append(anomaly.replace("_", "-"))
                if time_str:
                    parts.append(time_str.split()[1] if " " in time_str else time_str)
                txt = " / ".join(parts[:3])
                if txt:
                    self._ax.text(
                        mid_x,
                        mid_y,
                        txt,
                        fontsize=7,
                        color=color,
                        ha="center",
                        va="center",
                        bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.75),
                        zorder=4,
                    )
            else:
                # Other non-deployment relations: lighter, thinner arrows
                color = "#7F8C8D"
                self._ax.annotate(
                    "",
                    xy=(x2, y2),
                    xytext=(x1, y1),
                    arrowprops=dict(
                        arrowstyle="->",
                        color=color,
                        lw=1.4,
                        alpha=0.7,
                        shrinkA=8,
                        shrinkB=8,
                    ),
                    zorder=2,
                )

        # Node circles and labels
        node_radius = 0.22
        for nid, node in nodes.items():
            if nid not in positions:
                continue
            x, y = positions[nid]
            stage = node.stage
            status = node.status
            evidence = (getattr(node, "evidence", "") or "").strip()
            relation = (getattr(node, "relation", "") or "").strip()
            localized_match = bool(getattr(node, "localized_match", False))
            localized_time = (getattr(node, "localized_time", "") or "").strip()
            localized_severity = float(getattr(node, "localized_severity", 0.0) or 0.0)
            is_low_conf = status == "pruned" and "Low confidence" in evidence
            if is_low_conf:
                fill_color = STATUS_COLORS["low_confidence"]
            elif stage == "expand" and localized_match:
                fill_color = LOCALIZED_MATCH_FILL
            else:
                fill_color = STATUS_COLORS.get(status, STATUS_COLORS["candidate"])
            edge_color = (
                LOCALIZED_MATCH_EDGE
                if stage == "expand" and localized_match
                else STAGE_COLORS.get(stage, "#AAAAAA")
            )

            circle = mpatches.Circle(
                (x, y),
                node_radius,
                facecolor=fill_color,
                edgecolor=edge_color,
                linewidth=1.5,
                alpha=0.85,
                zorder=2,
            )
            self._ax.add_patch(circle)

            label_parts = [node.component or "?"]
            if stage == "localize":
                kpi_val = getattr(node, "kpi", None)
                kpi_list: list[str] = []
                if isinstance(kpi_val, str):
                    kpi_list = [k.strip() for k in kpi_val.split(",") if k.strip()]
                elif isinstance(kpi_val, (list, tuple)):
                    kpi_list = [str(k).strip() for k in kpi_val if str(k).strip()]
                if kpi_list:
                    label_parts.append("\n".join(kpi_list))
            elif stage in ("deep_dive", "expand"):
                rc_class = getattr(node, "root_cause_reason_class", None) or ""
                if (rc_class or "").strip():
                    label_parts.append((rc_class or "").strip()[:16])
                elif (node.reason or "").strip():
                    label_parts.append((node.reason or "").strip()[:16])
            elif node.reason and stage not in ("localize", "deep_dive", "expand"):
                label_parts.append((node.reason or "")[:16])

            # Append time (HH:MM or full string) so we can see when this node's
            # anomaly/decision is anchored in time.
            if getattr(node, "time", None):
                t = str(node.time)
                try:
                    # Prefer just the clock part if it's a full datetime.
                    t = t.split()[1] if " " in t else t
                except Exception:
                    pass
                label_parts.append(t)

            if getattr(node, "confidence", None) is not None and node.confidence > 0:
                pct = int(round(node.confidence * 100)) if node.confidence <= 1 else int(round(node.confidence))
                label_parts.append(f"{pct}%")

            # For expand nodes, show relation types (topology) only.
            if stage == "expand":
                if localized_match:
                    badge = "L1"
                    if localized_time:
                        t_loc = localized_time.split()[1] if " " in localized_time else localized_time
                        badge = f"{badge}@{t_loc}"
                    if localized_severity > 0:
                        badge = f"{badge} sev{int(round(localized_severity))}"
                    label_parts.append(badge)
                if relation:
                    # Show up to two relation lines for readability
                    rel_tokens = [t.strip() for t in re.split(r"[;,]", relation) if t.strip()]
                    rel_lines = []
                    for tok in rel_tokens:
                        rel_lines.append(tok[:28])
                        if len(rel_lines) >= 2:
                            break
                    if rel_lines:
                        label_parts.extend(rel_lines)

            label = "\n".join(label_parts)
            self._ax.text(x, y - node_radius - 0.08, label, ha="center", va="top", fontsize=7, wrap=True)

        # Deployment containment groups: draw rounded boxes around host + members.
        containment_groups = getattr(tree, "containment_groups", []) or []
        if containment_groups:
            from matplotlib.patches import FancyBboxPatch

            for group in containment_groups:
                try:
                    member_ids = group.get("member_node_ids") or []
                    if not isinstance(member_ids, list) or len(member_ids) < 2:
                        continue
                    pts = [positions[nid] for nid in member_ids if nid in positions]
                    if len(pts) < 2:
                        continue
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    pad_x = 0.5
                    pad_y = 0.6
                    min_x, max_x = min(xs) - pad_x, max(xs) + pad_x
                    min_y, max_y = min(ys) - pad_y, max(ys) + pad_y
                    width = max_x - min_x
                    height = max_y - min_y
                    host_label = str(group.get("container_component") or group.get("label") or "").strip()
                    box = FancyBboxPatch(
                        (min_x, min_y),
                        width,
                        height,
                        boxstyle="round,pad=0.25",
                        linewidth=1.3,
                        edgecolor="#2ECC71",
                        facecolor="none",
                        linestyle="--",
                        alpha=0.9,
                        zorder=1,
                    )
                    self._ax.add_patch(box)
                    if host_label:
                        self._ax.text(
                            min_x + width / 2.0,
                            max_y + 0.15,
                            host_label,
                            ha="center",
                            va="bottom",
                            fontsize=7,
                            color="#2ECC71",
                            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.8),
                            zorder=2,
                        )
                except Exception:
                    continue

        self._ax.set_xlim(min(p[0] for p in positions.values()) - 1.5, max(p[0] for p in positions.values()) + 1.5)
        self._ax.set_ylim(min(p[1] for p in positions.values()) - 1.2, max(p[1] for p in positions.values()) + 0.5)
        # Legend for relation overlays
        from matplotlib.lines import Line2D
        legend_handles = [
            Line2D([], [], color="#E74C3C", lw=2.4, label="trace_call (caller→callee)"),
            Line2D([], [], color="gray", lw=1.0, label="tree edge (parent→child)"),
            Line2D([], [], color="#2ECC71", lw=1.3, linestyle="--", label="deployment_contains (host box)"),
        ]
        self._ax.legend(handles=legend_handles, loc="upper right", fontsize=7, frameon=False)

        self._ax.set_title("RCA Search Tree (live)")
        self._fig.tight_layout()
