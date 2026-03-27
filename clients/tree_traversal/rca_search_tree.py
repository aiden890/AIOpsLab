"""Search tree data structure for staged RCA pipeline.

Records every node creation, pruning, and confirmation event
so that Manim can replay the search process as an animation.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class TreeNode:
    id: str
    stage: str          # "root" | "localize" | "deep_dive" | "expand"
    component: str
    reason: Optional[str] = None
    time: Optional[str] = None
    status: str = "candidate"   # "candidate" | "confirmed" | "pruned"
    confidence: float = 0.0
    evidence: str = ""
    parent_id: Optional[str] = None
    children: list[str] = field(default_factory=list)
    # One or more KPIs associated with this node (especially for localization).
    kpi: list[str] = field(default_factory=list)
    step: int = 0
    severity: float = 0.0      # from localization outlier (0–100); used to order candidates
    root_cause_reason_class: Optional[str] = None   # one of allowed possible_reasons; set on deep_dive
    relation: Optional[str] = None   # e.g. call_upstream / deploy_node / shared_resource (for expand stage)
    localized_match: bool = False
    localized_time: Optional[str] = None
    localized_severity: float = 0.0
    # Deep-dive enrichment is stored on the original node (localize/expand) when available.
    deep_dive_reason: Optional[str] = None
    deep_dive_reason_class: Optional[str] = None
    deep_dive_confidence: float = 0.0
    deep_dive_time: Optional[str] = None
    deep_dive_evidence: str = ""
    deep_dive_checked_reasons: list[dict] = field(default_factory=list)


class SearchTree:
    """Accumulates RCA search decisions as a tree with event log."""

    def __init__(self):
        self.nodes: dict[str, TreeNode] = {}
        self.events: list[dict] = []
        # Trace-level edges between components (caller → callee), independent
        # of search-tree parent/child relationships. Used for visualization.
        self.trace_edges: list[dict] = []
        # Directional non-tree relation edges between created search nodes.
        self.relation_edges: list[dict] = []
        # Visual containment groups (e.g. host contains deployed docker nodes).
        self.containment_groups: list[dict] = []
        self._step = 0
        self._counters: dict[str, int] = {}

        root = TreeNode(
            id="root", stage="root", component="System Failure",
            status="active",
        )
        self.nodes["root"] = root
        self._record("create", "root")

    # ── internal helpers ──────────────────────────────────────────────

    def _record(self, action: str, node_id: str, **extra):
        self.events.append({
            "step": self._step,
            "action": action,       # create | prune | confirm
            "node_id": node_id,
            "ts": time.time(),
            **extra,
        })

    def _next_id(self, stage: str) -> str:
        cnt = self._counters.get(stage, 0)
        self._counters[stage] = cnt + 1
        return f"{stage}_{cnt}"

    # ── mutations ─────────────────────────────────────────────────────

    def add_candidate(
        self,
        stage: str,
        component: str,
        reason: str | None = None,
        time_str: str | None = None,
        parent_id: str = "root",
        kpi: str | list[str] | None = None,
        evidence: str = "",
        confidence: float = 0.0,
        severity: float = 0.0,
        root_cause_reason_class: str | None = None,
        relation: str | None = None,
        localized_match: bool = False,
        localized_time: str | None = None,
        localized_severity: float = 0.0,
    ) -> str:
        self._step += 1
        nid = self._next_id(stage)
        # Normalize KPI input to a list of strings.
        kpi_list: list[str] = []
        if isinstance(kpi, str):
            kpi_list = [kpi]
        elif isinstance(kpi, (list, tuple)):
            kpi_list = [str(x) for x in kpi]
        node = TreeNode(
            id=nid, stage=stage, component=component,
            reason=reason, time=time_str, status="candidate",
            confidence=confidence, parent_id=parent_id,
            kpi=kpi_list, evidence=evidence, step=self._step,
            severity=severity,
            root_cause_reason_class=root_cause_reason_class,
            relation=relation,
            localized_match=localized_match,
            localized_time=localized_time,
            localized_severity=localized_severity,
        )
        self.nodes[nid] = node
        if parent_id in self.nodes:
            self.nodes[parent_id].children.append(nid)
        self._record("create", nid)
        return nid

    def add_trace_edge(self, src_component: str, dst_component: str, **extra):
        """Record a trace-level edge between two components (caller → callee)."""
        payload = {"from": src_component, "to": dst_component, **extra}
        self.trace_edges.append(payload)

    def add_relation_edge(
        self,
        src_node_id: str | None,
        dst_node_id: str | None,
        *,
        src_component: str | None = None,
        dst_component: str | None = None,
        relation_family: str | None = None,
        relation_type: str | None = None,
        anomaly_type: str | None = None,
        time: str | None = None,
        label: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Record a typed non-tree relation edge between search nodes."""
        edge = {
            "src_node_id": src_node_id,
            "dst_node_id": dst_node_id,
            "src_component": src_component,
            "dst_component": dst_component,
            "relation_family": relation_family,
            "relation_type": relation_type,
            "anomaly_type": anomaly_type,
            "time": time,
            "label": label,
            "metadata": metadata or {},
        }
        dedupe_key = (
            edge["src_node_id"],
            edge["dst_node_id"],
            edge["src_component"],
            edge["dst_component"],
            edge["relation_family"],
            edge["relation_type"],
            edge["anomaly_type"],
            edge["time"],
        )
        for existing in self.relation_edges:
            existing_key = (
                existing.get("src_node_id"),
                existing.get("dst_node_id"),
                existing.get("src_component"),
                existing.get("dst_component"),
                existing.get("relation_family"),
                existing.get("relation_type"),
                existing.get("anomaly_type"),
                existing.get("time"),
            )
            if existing_key == dedupe_key:
                return
        self.relation_edges.append(edge)

    def upsert_containment_group(
        self,
        container_node_id: str | None,
        member_node_ids: list[str],
        *,
        container_component: str | None = None,
        relation_family: str = "deployment_contains",
        relation_type: str = "contains",
        label: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Record or update a visual containment group."""
        member_ids = [
            mid for mid in member_node_ids
            if mid and mid in self.nodes
        ]
        if container_node_id and container_node_id in self.nodes and container_node_id not in member_ids:
            member_ids.insert(0, container_node_id)
        if not member_ids:
            return

        payload = {
            "container_node_id": container_node_id,
            "container_component": container_component,
            "member_node_ids": member_ids,
            "relation_family": relation_family,
            "relation_type": relation_type,
            "label": label or container_component or "",
            "metadata": metadata or {},
        }
        for existing in self.containment_groups:
            same_container = existing.get("container_node_id") == container_node_id
            if container_node_id is None:
                same_container = same_container and (
                    existing.get("container_component") == container_component
                )
            if same_container and existing.get("relation_family") == relation_family:
                merged = list(dict.fromkeys(existing.get("member_node_ids", []) + member_ids))
                existing["member_node_ids"] = merged
                if payload["label"]:
                    existing["label"] = payload["label"]
                if payload["container_component"]:
                    existing["container_component"] = payload["container_component"]
                existing_meta = existing.setdefault("metadata", {})
                existing_meta.update(payload["metadata"])
                return
        self.containment_groups.append(payload)

    def prune(self, node_id: str, reason: str = ""):
        self._step += 1
        node = self.nodes.get(node_id)
        if node:
            node.status = "pruned"
            if reason:
                node.evidence = reason
            self._record("prune", node_id, reason=reason)

    def confirm(self, node_id: str, confidence: float = 1.0,
                evidence: str = "", reason: str | None = None,
                root_cause_reason_class: str | None = None):
        self._step += 1
        node = self.nodes.get(node_id)
        if node:
            node.status = "confirmed"
            node.confidence = confidence
            if evidence:
                node.evidence = evidence
            if reason is not None:
                node.reason = reason
            if root_cause_reason_class is not None:
                node.root_cause_reason_class = root_cause_reason_class
            self._record("confirm", node_id, confidence=confidence)

    # ── queries ───────────────────────────────────────────────────────

    def get_by_stage(self, stage: str, status: str | None = None) -> list[TreeNode]:
        nodes = [n for n in self.nodes.values() if n.stage == stage]
        if status:
            nodes = [n for n in nodes if n.status == status]
        return nodes

    def get_confirmed(self, stage: str | None = None) -> list[TreeNode]:
        nodes = [n for n in self.nodes.values() if n.status == "confirmed"]
        if stage:
            nodes = [n for n in nodes if n.stage == stage]
        return sorted(nodes, key=lambda n: -n.confidence)

    def get_best(self) -> TreeNode | None:
        confirmed = self.get_confirmed()
        return confirmed[0] if confirmed else None

    def get_best_or_highest_confidence(self) -> TreeNode | None:
        """Return best confirmed node, or if none, the node with highest confidence (e.g. after full traversal with no confirm)."""
        best = self.get_best()
        if best is not None:
            return best
        # No confirmed: pick among nodes with confidence/deep-dive evidence.
        candidates = [
            n for n in self.nodes.values()
            if (
                (float(getattr(n, "confidence", 0.0) or 0.0) > 0.0)
                or (float(getattr(n, "deep_dive_confidence", 0.0) or 0.0) > 0.0)
            )
            and n.id != "root"
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda n: (
                float(getattr(n, "deep_dive_confidence", 0.0) or 0.0),
                float(getattr(n, "confidence", 0.0) or 0.0),
                n.stage == "expand",
            ),
        )

    # ── persistence ───────────────────────────────────────────────────

    def save(self, path: str | Path):
        data = {
            "nodes": [
                {
                    "id": n.id, "stage": n.stage, "component": n.component,
                    "reason": n.reason, "time": n.time, "status": n.status,
                    "confidence": n.confidence, "evidence": n.evidence,
                    "parent_id": n.parent_id, "children": n.children,
                    "kpi": n.kpi, "step": n.step, "severity": getattr(n, "severity", 0.0),
                    "root_cause_reason_class": getattr(n, "root_cause_reason_class", None),
                    "relation": getattr(n, "relation", None),
                    "localized_match": getattr(n, "localized_match", False),
                    "localized_time": getattr(n, "localized_time", None),
                    "localized_severity": getattr(n, "localized_severity", 0.0),
                    "deep_dive_reason": getattr(n, "deep_dive_reason", None),
                    "deep_dive_reason_class": getattr(n, "deep_dive_reason_class", None),
                    "deep_dive_confidence": getattr(n, "deep_dive_confidence", 0.0),
                    "deep_dive_time": getattr(n, "deep_dive_time", None),
                    "deep_dive_evidence": getattr(n, "deep_dive_evidence", ""),
                    "deep_dive_checked_reasons": getattr(n, "deep_dive_checked_reasons", []),
                }
                for n in self.nodes.values()
            ],
            "events": self.events,
            "trace_edges": self.trace_edges,
            "relation_edges": self.relation_edges,
            "containment_groups": self.containment_groups,
        }
        # Use default=str so datetime/pandas Timestamp and other non-JSON-native
        # scalar types are serialized as strings instead of raising.
        Path(path).write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str)
        )

    @classmethod
    def load(cls, path: str | Path) -> SearchTree:
        data = json.loads(Path(path).read_text())
        tree = cls.__new__(cls)
        tree.nodes = {}
        tree.events = data["events"]
        tree.trace_edges = data.get("trace_edges", [])
        tree.relation_edges = data.get("relation_edges", [])
        tree.containment_groups = data.get("containment_groups", [])
        tree._step = max((e["step"] for e in tree.events), default=0)
        tree._counters = {}
        for nd in data["nodes"]:
            raw_kpi = nd.get("kpi")
            if isinstance(raw_kpi, str):
                kpi_list = [raw_kpi]
            elif isinstance(raw_kpi, (list, tuple)):
                kpi_list = [str(x) for x in raw_kpi]
            else:
                kpi_list = []
            node = TreeNode(
                id=nd["id"], stage=nd["stage"], component=nd["component"],
                reason=nd.get("reason"), time=nd.get("time"),
                status=nd["status"], confidence=nd.get("confidence", 0),
                evidence=nd.get("evidence", ""),
                parent_id=nd.get("parent_id"),
                children=nd.get("children", []),
                kpi=kpi_list, step=nd.get("step", 0),
                severity=float(nd.get("severity", 0.0)),
                root_cause_reason_class=nd.get("root_cause_reason_class"),
                relation=nd.get("relation"),
                localized_match=bool(nd.get("localized_match", False)),
                localized_time=nd.get("localized_time"),
                localized_severity=float(nd.get("localized_severity", 0.0)),
                deep_dive_reason=nd.get("deep_dive_reason"),
                deep_dive_reason_class=nd.get("deep_dive_reason_class"),
                deep_dive_confidence=float(nd.get("deep_dive_confidence", 0.0) or 0.0),
                deep_dive_time=nd.get("deep_dive_time"),
                deep_dive_evidence=nd.get("deep_dive_evidence", ""),
                deep_dive_checked_reasons=list(nd.get("deep_dive_checked_reasons", []) or []),
            )
            tree.nodes[node.id] = node
        return tree
