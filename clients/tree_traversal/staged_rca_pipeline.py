"""5-stage RCA pipeline: Localize → Full Expand → optional Deep Dive → Global Shortlist+DeepDive → Global Judge.

After localization, the pipeline builds an anomaly dependency tree, may run
optional per-node deep dive, runs a global top-k shortlist + focused deep dive,
then uses a final global judge over the whole search tree to pick the root cause.

Dataset-agnostic: all KPI/reason/component knowledge comes from
a DatasetProfile, so the same pipeline works for Market, Telecom, Bank.

Logging:
  - Console (logger): minimal progress — stage transitions only
  - session.log (sprint): full detail — every action, response, evidence
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from aiopslab.orchestrator.parser import ResponseParser
from aiopslab.orchestrator.static_actions.trace_path_renderer import (
    get_trace_path_renderer,
)

from clients.openrca_rca.api_router import get_chat_completion
from clients.tree_traversal.controller_stage import run_controller_stage
from clients.tree_traversal.dataset_profile import DatasetProfile
from clients.tree_traversal.graph import (
    get_graphs,
    get_related_components_for_expand,
    get_related_components_with_relations,
)
from clients.tree_traversal.rca_search_tree import SearchTree, TreeNode

logger = logging.getLogger("staged_rca_pipeline")

_LOCALIZATION_DEP_CLUSTER_WINDOW_MINUTES = 10
_LOCALIZATION_DEP_RELATION_ORDER = {"call": 0, "shared": 1, "deploy": 2}
_LOCALIZATION_DEP_RELATION_COLORS = {
    "call": "#4A90D9",
    "shared": "#8E44AD",
    "deploy": "#E67E22",
}
_LOCALIZATION_DEP_CLUSTER_GAP = 4.2
_LOCALIZATION_DEP_BOX_WIDTH = 2.6
_LOCALIZATION_DEP_BOX_HEIGHT = 0.62

# ── LLM prompt templates (dataset-independent) ──────────────────────

_LOCALIZE_SUFFIX = """
IMPORTANT: For each outlier, include all of the following:

- "time": **MUST** be exactly in format YYYY-MM-DD HH:MM:SS (e.g. "2022-03-20 02:22:00"). If the chart axis shows only time (e.g. 02:22), use the query date for the date part so the full timestamp is YYYY-MM-DD HH:MM:SS. Do NOT use "approx", "~", or time-only strings like "02:22:00" without the date.
- "reason_hint": the most likely failure reason based on the KPI
- "duration_approx": how long the anomaly lasts (e.g., "~2 min", "several minutes", "sustained")
- "kpi": the metric/KPI name shown on the chart (e.g., "system.cpu.pct_usage", "container_memory_usage_MB")
- "anomaly_value": the anomalous value or range (e.g., "35–40%", "spike to 30%", "drops to ~12%")
- "anomaly_type": one of: "spike", "drop", "step_up", "step_down", "cyclic", "sustained_high", "sustained_low", "other"
- "severity": numeric score from 0 to 100 (0 = negligible, 100 = very strong anomaly).
- "value_is_problematic": true if the ACTUAL KPI level/range would be operationally problematic for this KPI, false if it only looks visually different but is still likely harmless.
- "value_judgment": brief explanation of why the actual KPI value/range is problematic or not problematic. You MUST consider KPI semantics, not just shape.

IMPORTANT: Do not judge only by visual difference. Also ask:
- Is the actual KPI VALUE high/low enough to matter operationally for this KPI?
- Could this be just a small harmless level shift with no real impact?
- For percentage/saturation KPIs, higher near saturation is more problematic.
- For latency KPIs, a visible spike can matter even if the absolute number is not huge, but tiny changes should not be treated as serious.
- For memory/usage KPIs, a tiny step change is not necessarily problematic unless the absolute level or relative jump is meaningfully large.

Return JSON:
{
  "outliers": [
    {
      "component": "<name>",
      "change": "<what changed>",
      "severity": <number 0-100>,
      "time": "<YYYY-MM-DD HH:MM:SS>",
      "reason_hint": "<reason from list>",
      "duration_approx": "<e.g. ~2 min or sustained>",
      "kpi": "<metric name>",
      "anomaly_value": "<e.g. 35-40% or spike to 30%>",
      "anomaly_type": "spike|drop|step_up|step_down|cyclic|sustained_high|sustained_low|other"
      "value_is_problematic": true | false,
      "value_judgment": "<brief KPI-value-based justification>"
    }
  ]
}
"""

_TRACE_PATH_VISION_CRITIC = """\
You are analyzing a trace dependency path chart for Root Cause Analysis.

The chart shows:
- A directed graph of caller→callee (e.g. os_022→docker_002→db_007)
- Edge labels:
  - "E XX% DYm xN" = error-rate anomaly edge
  - "G XXXms DYm xN" = network-gap anomaly edge
  - remote node labels "R XXXms DYm" = remote-process-time anomaly on that callee node
- Gray edges are normal; colored edges indicate anomalies

**Task**: Identify one or more components that are most likely root causes.
Return one or more candidates when several appear equally suspicious.

KPI mapping requirements:
- For network gap anomalies, prefer OS-level components (os_*). If you first see docker_* as suspicious for network behavior, use deployment mapping (host → components) to map it to the hosting OS and report that OS component.
- For remote process time anomalies, report docker_* components (callee service containers), not OS.
- For error-edge anomalies, report the most causally plausible component in the edge neighborhood.
- A longer remote_process_time on a caller can be a propagated effect from a slower downstream callee; do not assume caller is root cause based on caller remote time alone.

**Output**: Return JSON with one or more candidates. For each candidate:
- "component": exact name (e.g. docker_002, os_022, db_007)
- "time": onset time in YYYY-MM-DD HH:MM:SS (use chart labels like "21:16" with query date)
- "severity": 0-100 (higher = more likely root cause; use 80+ for strong candidates)
- "reason_hint": e.g. "network delay", "CPU fault", "db close"
- "anomalous_kpi": one of ["error_rate", "network_gap", "remote_process_time"]
- "anomaly_value": concrete value seen on chart (e.g. "E 100%", "G 4200ms", "R 6100ms")
- "value_is_problematic": true

Return JSON:
{
  "outliers": [
    {"component": "<name>", "time": "<YYYY-MM-DD HH:MM:SS>", "severity": <0-100>, "reason_hint": "<reason>", "anomalous_kpi": "<error_rate|network_gap|remote_process_time>", "anomaly_value": "<value>", "value_is_problematic": true, "change": "<brief>"},
    ...
  ]
}
"""

_DEEP_DIVE_PROMPT = """\
You are an RCA analyst. Given the following telemetry analysis result,
determine if the component "{component}" experienced "{reason}" around {time}.

Analysis result:
{evidence}

Score your confidence from 0.0 to 1.0:
- 1.0 = clear, sustained anomaly matching this reason
- 0.5 = some signal but ambiguous
- 0.0 = no evidence at all

Return ONLY a JSON object:
{{"confidence": <float>, "explanation": "<brief reason>"}}
"""

_EXPAND_PROMPT = """\
You are an RCA analyst investigating a hypothesis:
  Component: {component}
  Reason: {reason}
  Time: {time}

I shifted the time window by {offset} minutes and re-checked.
Result at shifted time:
{shifted_evidence}

Original evidence:
{original_evidence}

Determine:
1. Is the anomaly already present at the shifted time? (precedes → likely root cause)
2. Or does it only appear after? (follows → likely symptom)
3. Are there other components showing anomalies that could be the actual root cause?

Return ONLY a JSON object:
{{"verdict": "root_cause|symptom|unclear",
  "confidence": <float 0-1>,
  "related_components": ["<component_name>", ...],
  "explanation": "<brief>"}}
"""

_LOCALIZE_FALLBACK_PROMPT = """\
You are helping with RCA localization fallback.

The normal localization filter produced zero candidates. Below is a pool of all localization findings seen so far, including findings that were filtered out because their KPI value was judged "not problematic".

Your job:
1. Pick up to 3 candidate components for fallback localization.
2. Prioritize components that appear repeatedly across multiple KPIs, have high severity, and show temporally clustered anomalies.
3. value_is_problematic=false does NOT automatically disqualify a component here. When no normal candidates survived, repeated/high-severity non-problematic-looking anomalies are still useful fallback hypotheses.
4. Prefer components with multiple corroborating signals over isolated one-off weak signals.

Return ONLY JSON:
{
  "candidates": [
    {
      "component": "<component name>",
      "time": "<YYYY-MM-DD HH:MM:SS>",
      "reason_hint": "<brief hypothesis>",
      "problematic_kpis": ["<kpi1>", "<kpi2>", "..."],
      "confidence": <float 0-1>,
      "why": "<brief explanation>"
    }
  ]
}
"""

# Deep dive controller: narrow to one root cause reason class in ±5 min window
_DEEP_DIVE_WORKFLOW = """\
**Goal** — In localization, a **specific KPI** showed an anomaly for this component. Your job is to **narrow down** to **which root cause reason** (exactly one from the allowed list) is the **underlying cause**, not just the first metric that moved. The anomalous KPI may be a **symptom** (e.g. trace latency spike caused by CPU fault or DB slowdown). In deep dive you should work **only with tabular telemetry via execute()** (no new charts), then conclude with one root cause class or low confidence to prune.

**Step 1 — Multiple KPIs (map to root cause classes) via execute()**
Do **not** only re-check the KPI that was anomalous. Check **several metrics** that correspond to different root cause classes (e.g. CPU, memory, disk I/O, network, DB sessions). Use `execute()` to load and analyze the telemetry tables (metric_*.csv, trace_span.csv, log files) for this component (and peers) in the **t-5min to t+5min** window. For each root cause class you consider, look at the metric that best represents its **cause signal** (e.g. CPU for "CPU fault", DB sessions for "db connection limit") and see whether it shows a consistent anomaly that can explain the others. Treat pure latency KPIs (e.g. generic trace latency) as **symptoms** unless you can show there is no deeper cause in CPU / DB / network / disk.
For network-related conclusions ("network loss" / "network delay"), you MUST explicitly compute and report **network_gap = elapsedTime(callType='CSF') - elapsedTime(callType='RemoteProcess')** from telemetry/traces and use that value in your judgment. In trace semantics, **CSF** is caller-side framework/network-facing span time and **RemoteProcess** is callee-side remote processing span time. To obtain caller-callee network gap correctly, map CSF and RemoteProcess on the same call chain using trace linkage (prefer `pid` ↔ `id` with `traceId`), then subtract their elapsedTime values. In Telecom, treat **network_gap > 100000** as strong evidence for severe network fault (loss-prone); if network_gap is not high enough or not sustained, do not conclude network loss by default.

**Step 2 — execute() for custom analysis (only)**
Use `execute()` whenever you need:
- to compute statistics over KPIs (e.g. baseline vs fault-window mean, max, percentiles),
- to correlate multiple KPIs or components,
- to filter/slice time windows and components.
Your execute instruction must describe **what analysis to run on the telemetry data** (e.g. "compute X from metric Y for component Z in window W"). Do not pass image paths; describe the analysis in natural language. **Do not call visualization actions like get_kpi_peer_graph, get_trace_latency_peer_graph, get_trace_error_peer_graph, or get_trace_peer_graph during deep dive.**

**Step 3 — Traces and logs (if relevant, via execute())**
Use `execute()` to inspect trace tables and logs for latency/errors and dependency (e.g. group-by cmdb_id, bucketed latency/error rate, etc.). Use `execute()` to inspect logs for errors or events in the time window. Use traces mainly to **locate where in the call path the slowdown appears**, then look for the **cause KPIs** (CPU, DB, network, disk) on that component.

**Step 4 — Conclude (root cause class + earliest anomaly time)**
Pick **exactly one** root cause class from the allowed list that best explains **why** the system slowed down (CPU / DB / network / disk / etc.), even if another KPI (e.g. trace latency) fired slightly earlier. Do **not** choose a pure symptom KPI (like generic latency) as the root cause if there is a plausible deeper cause in other KPIs. Also, based on your execute() analysis and KPIs, estimate the **earliest timestamp within the episode when this component's own KPIs clearly became abnormal** (e.g. CPU spike start, DB session saturation start). This should be within the query time range. If you cannot refine it, reuse the localization time.

When concluding, output: confidence, **reason** (exactly one from the list — copy the string literally), **time** (earliest anomaly start for this component in format YYYY-MM-DD HH:MM:SS), and explanation. Do not use execute() to conclude — state confidence, time, reason, and explanation yourself.
"""

_DEEP_DIVE_SYSTEM_TEMPLATE = """\
## Role Definition

You are a DevOps engineer in the **deep-dive** stage of RCA. In localization, a **specific KPI** showed an anomaly for a component. Your job is to **narrow down** to **which root cause reason** (exactly one from the allowed list below) actually caused that problem — not just to confirm the initial hint. Use **multiple KPIs** (not only the one that was anomalous) and **execute()** when you need custom analysis on telemetry; then conclude with one root cause class or low confidence to prune.

## Possible root cause classes

You MUST choose exactly one of the following as `reason` when you submit. No other value is accepted.

{possible_reasons_list}

## Background

{background}

## Workflow

Follow the steps below. Use actions to gather evidence (multiple KPIs, execute, traces/logs as needed), then conclude.

{workflow}

## Available actions

{action_list}

  "thought": "<your reasoning and what you will do next>",
  "action": "<action_name>",
  "args": {{"<param>": "<value>", ...}}
}}

**When concluding** (you have enough evidence to pick a root cause class or to reject the candidate):
{{
  "thought": "<final reasoning>",
  "action": "submit",
  "confidence": <float 0-1>,
  "time": "<YYYY-MM-DD HH:MM:SS earliest anomaly time for this component (within query range)>",
  "reason": "<exactly one from the Possible root cause classes list above — copy the string literally>",
  "explanation": "<brief reason>",
  "other_suspect": ["<component>", ...]
}}
- **reason**: Must be exactly one of the possible root cause classes listed above (copy the string literally). If you conclude the candidate is not a real problem, use low confidence and omit reason or leave empty (candidate will be pruned).
- **other_suspect**: Optional; list other components you still find suspicious. Omit or use [] if none.
"""

# Expand controller: same ReAct format (thought, action, args; then per-component anomaly summary)
_EXPAND_WORKFLOW = """\
**Step 1 — Inspect topology-related candidates with execute() only**
The background lists topology-related components (from call/deployment/shared-resource graphs). Treat these as the **candidate set** for expand. Expand is an **anomaly discovery/filtering stage**, not the final root-cause decision stage. If the candidates are split into component-family batches (for example DB, Docker, OS), analyze each batch independently and only compare components within that batch. For each candidate, use `execute()` to inspect its **own KPIs** in a wider window around the hypothesis time: default to **t-5min to t+5min** so you can see lead-up, onset, and immediate aftermath of the suspected anomaly. Compare that focused window against an earlier baseline such as **t-15min to t-5min** when available, and also use the **full query window** to reject periodic/cyclic patterns. Focus on KPIs appropriate to the level (node/pod/service: CPU, memory, disk I/O, ICMP_ping, DB sessions, queues, etc.). If the background provides an explicit KPI checklist for the current batch, use that list broadly rather than checking only one representative KPI. Prefer the **earliest sustained deviation** that clearly separates the candidate from peers; a single-sample spike without persistence is weak evidence and should usually not be treated as a strong anomaly. Very important: judge anomaly strength and `value_is_problematic` by the **actual KPI magnitude/range**, not just by relative lift, z-score, or anomaly score. A huge ratio from near-zero baseline does **not** automatically mean the KPI level is operationally problematic.
For Market pod-level analysis, remember that `metric_container.csv` often stores `cmdb_id` as `node-X.<pod_name>` while trace/logs use `<pod_name>`. In execute() queries, always match both exact and suffix forms.

**Step 2 — Use trace tables with execute() only**
Use `execute()` on trace tables to inspect latency, error rate, and span volume in the same **t-5min to t+5min** window and baseline window. Determine where along the call path latency/errors first appear or concentrate, and combine that with KPI analysis from Step 1 to distinguish components with their **own anomaly signal** from components that only show propagated effects. Do **not** use visualization actions during expand; rely on execute-derived summaries/statistics only.

**Step 3 — Conclude (filter candidates)**
Once you have enough evidence, output your conclusion as a per-component anomaly table. For each candidate you inspected, decide whether it has a strong anomaly in its OWN KPIs in the **t-5min to t+5min** window relative to baseline. Only components that you judge as having a strong anomaly worth sending to the next stage should be marked has_anomaly=true, and provide a confidence score in [0.0, 1.0] for that anomaly assessment. For each component entry, also return the **strongest anomalous KPI name**, the **actual anomalous value/range** (not just ratios, z-scores, or anomaly_score summaries), and whether the KPI's **actual numeric level/range would be a real operational problem when interpreted according to that KPI's meaning** (`value_is_problematic`). Include a brief `value_judgment` explaining the KPI semantics behind that decision: for example, whether the value implies saturation, exhaustion, serious latency, backlog, service down state, or whether it is elevated but still likely harmless for that KPI. Components that look normal, purely periodic, or only show propagated latency/effects should have has_anomaly=false (or be omitted) and confidence near 0. Do **not** make the final root-cause decision in expand; deep dive will decide that later.
"""

_EXPAND_SYSTEM_TEMPLATE = """\
## Role Definition

You are a DevOps engineer in the **expand** stage of RCA.
Your goal is to identify which topology-related components show a strong anomaly signal and should advance to later investigation. Expand is for anomaly discovery/filtering, not for making the final root-cause decision.

## Background

{background}

## Workflow

Follow the steps below. Use actions to gather evidence, then conclude.

{workflow}

## Available actions

{action_list}

At each turn, respond ONLY with a single JSON object (no markdown, no code block). No other text.

**While gathering evidence:**
{{
  "thought": "<your reasoning and what you will do next>",
  "action": "<action_name>",
  "args": {{"<param>": "<value>", ...}}
}}

**When concluding:**
{{
  "thought": "<final reasoning>",
  "action": "submit",
  "verdict": "strong_anomaly|weak_anomaly|no_anomaly|unclear",
  "components": [
    {{
      "component": "<name>",
      "time": "<YYYY-MM-DD HH:MM:SS at which its KPIs are most anomalous>",
      "has_anomaly": true | false,
      "confidence": <float 0-1 anomaly confidence for this component>,
      "anomalous_kpi": "<single strongest KPI name for this component>",
      "anomaly_value": "<actual anomalous absolute value or range for that KPI; e.g. 'peaks at ~20', 'sustained at ~78-82', 'queue rises to ~150', not 'ratio≈580x'>",
      "value_is_problematic": true | false,
      "value_judgment": "<brief KPI-semantics-based reason why this numeric level/range is or is not truly operationally problematic>",
      "clues": "<brief KPI-based clues for why you decided anomaly vs normal>"
    }},
    ...
  ],
  "explanation": "<overall brief summary>"
}}
Only components with both has_anomaly=true and value_is_problematic=true will be expanded as new candidates. Components that are normal, only weakly anomalous, or not truly problematic in KPI semantics should have has_anomaly=false, value_is_problematic=false, or be omitted from the list.
"""

_GLOBAL_JUDGE_SYSTEM_TEMPLATE = """\
## Role Definition

You are the final **global judge** for RCA.
You will receive the full search tree after localization and expand, and
possibly after optional deep dive. Your job is to pick the **single best root
cause node already present in the tree**.

## Possible root cause classes

You MUST choose exactly one of the following as `reason` when you submit. No other value is accepted.

{possible_reasons_list}

## Decision principles

- Pick an existing node from the tree by `node_id`. Do not invent a new node.
- Prefer a node whose **own KPI evidence** looks operationally problematic, not just visually different.
- Prefer nodes that can **explain downstream anomalies** on their descendants or related branches.
- Do not over-prefer generic symptom nodes if there is a deeper KPI-based cause such as CPU, DB, network, disk, queue, or service-down evidence.
- Use topology/path information, relation types, localization severity, and expand evidence together.
- If deep-dive evidence is present (either dedicated deep-dive nodes or node-level deep_dive_* fields), treat it as stronger per-node evidence than raw reason hints.
- Use the provided time on the chosen node unless you can refine it slightly within the same episode; if you refine it, keep it within the query window.

## Network fault hints (caller-callee trace path)

- **network loss**: Often manifests as latency symptoms on the **caller** side (e.g. caller→callee edge shows high latency because packets are lost/retransmitted). The caller may appear anomalous due to elevated latency even though the fault is on the link or callee's network.
- **network delay**: Can affect **both caller and callee**; elevated latency may appear on edges in both directions. Consider the node where delay is introduced (e.g. the host/network between them) rather than only the caller.
- For network reasons, verify **network_gap = elapsedTime(callType='CSF') - elapsedTime(callType='RemoteProcess')** first.
- If trace evidence shows **network_gap > 100000**, interpret it as strong **network loss** evidence.
- If trace evidence shows **network_gap <= 100000**, prefer **network delay** only when sustained delay evidence exists; otherwise avoid concluding network fault.

## DB fault hints

- **db close**: Only consider when **On_Off_State = 0** (DB is down). If On_Off_State is 1 or the DB KPI does not show a down state, prefer other reasons (e.g. db connection limit).
- **On_Off_State absent from anomaly KPIs**: If db On_Off_State is not listed among the anomalous KPIs for a component, it means **1** (DB is up/normal). Do not infer db close in that case.

Return ONLY one JSON object:
{{
  "decision": "final|investigate_more",
  "node_id": "<existing node id from the tree>",
  "component": "<component name of the chosen node>",
  "reason": "<exactly one from the allowed list above>",
  "time": "<YYYY-MM-DD HH:MM:SS>",
  "confidence": <float 0-1>,
  "explanation": "<brief causal justification>",
  "why_not_others": "<brief reason why competing nodes are less likely>",
  "supporting_path": ["<node_id>", "..."],
  "follow_up_candidates": [
    {{
      "node_id": "<existing node id to investigate next>",
      "component": "<component name>",
      "time": "<YYYY-MM-DD HH:MM:SS>",
      "why": "<why more deep dive is needed>"
    }}
  ]
}}

Rules:
- If decision is "final", fill node_id/component/reason/time/confidence and optionally supporting_path.
- If decision is "investigate_more", provide 1-3 follow_up_candidates and leave final fields empty or minimal.
"""

_GLOBAL_SHORTLIST_SYSTEM_TEMPLATE = """\
## Role Definition

You are the **global pre-judge** for RCA.
You will receive the full RCA tree and must pick the most plausible **Top-{top_k} candidate nodes**
for final deep-dive verification.

## Decision principles

- Pick only existing tree nodes by `node_id`; do not invent new nodes.
- Prefer candidates with strong own-KPI anomalies and causal explanatory power over pure symptoms.
- Use localization severity, expand evidence, and relation/path information together.
- Include candidate diversity when plausible (avoid returning near-duplicates unless strongly justified).
- For network-related candidates, verify/consider whether evidence mentions **network_gap = elapsedTime(callType='CSF') - elapsedTime(callType='RemoteProcess')**
  and whether it exceeds **100000**.

Return ONLY one JSON object:
{{
  "candidates": [
    {{
      "node_id": "<existing node id>",
      "component": "<component>",
      "time": "<YYYY-MM-DD HH:MM:SS>",
      "reason_hint": "<brief reason hint>",
      "score": <float 0-1>,
      "why": "<brief rationale>"
    }}
  ]
}}
"""


# =====================================================================
# Pipeline
# =====================================================================

class StagedRCAPipeline:
    """Orchestrates staged RCA with tree recording.

    Optional: render_localization_timeline (Manim time-on-x graph after stage1),
    use_controller_deep_dive / use_controller_expand for multi-turn
    controller↔action loops. When using controller stages, pass problem
    so perform_action can be invoked.
    """

    def __init__(
        self,
        actions,                    # StaticRCAActionsWithExecutor
        llm_configs: dict,          # from load_config(api_config_path)
        profile: DatasetProfile,    # dataset-specific knowledge
        namespace: str,
        save_dir: str,
        time_range: dict | None = None,
        sprint=None,                # SessionPrint for logging
        problem=None,               # required for controller stages (perform_action)
        render_localization_timeline: bool = False,
        use_controller_deep_dive: bool = True,
        use_controller_expand: bool = True,   # default: use controller-based expand
        enable_expand: bool = True,           # whether Stage 3 expand is enabled at all
        enable_deep_dive: bool = False,       # whether Stage 2 per-node deep dive runs
        expand_max_hops: int = 2,             # 1 = localization→expand only; 2 = +2nd hop from expand nodes
        live_viewer=None,           # LiveTreeViewer or list of them (timeline + tree)
    ):
        self.actions = actions
        self.configs = llm_configs
        self.profile = profile
        self.namespace = namespace
        self.save_dir = Path(save_dir)
        self.time_range = time_range or {}
        self.tree = SearchTree()
        self.sprint = sprint
        self.problem = problem
        self.render_localization_timeline = render_localization_timeline
        self.use_controller_deep_dive = use_controller_deep_dive
        self.use_controller_expand = use_controller_expand
        self.enable_expand = enable_expand
        self.enable_deep_dive = enable_deep_dive
        self.expand_max_hops = max(1, min(expand_max_hops, 2))
        self.live_viewer = live_viewer
        self._controller_action_lock = Lock()
        self._expand_cache_lock = Lock()
        self._expand_verdict_cache: dict[str, list[dict]] = {}
        self._trace_anomaly_edges: list[dict] = []
        # Keep filtered-out localization metric hints as auxiliary evidence.
        # key: component, value: list[dict(change, duration_approx, anomaly_value, ...)]
        self._filtered_localize_aux: dict[str, list[dict]] = {}
        # Pool of all localization findings seen so far. Used only when the
        # normal localization gates produce zero candidates.
        self._localize_fallback_pool: list[dict] = []

        # Build full vision critic prompt (base + structured output suffix)
        self._critic_prompt = profile.vision_critic_prompt + _LOCALIZE_SUFFIX

    # ── sprint helpers & live view ────────────────────────────────────

    def _log_agent(self, text: str, step_boundary: bool = False):
        """Log agent message. Use step_boundary=True only for stage headers (Step N)."""
        if not self.sprint:
            return
        if step_boundary:
            self.sprint.agent(text)
        else:
            self.sprint.agent_detail(text)

    def _log_service(self, text: str):
        """Log observation (detail only; does not start a new step)."""
        if self.sprint:
            self.sprint.service_detail(text)

    def _update_live_view(self):
        """Update matplotlib live viewer if enabled."""
        if self.live_viewer:
            viewers = self.live_viewer if isinstance(self.live_viewer, list) else [self.live_viewer]
            for v in viewers:
                v.update(self.tree)

    def _store_filtered_localize_aux(self, items: list[dict]) -> None:
        """Store filtered localization outliers as auxiliary per-component evidence."""
        if not items:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            comp = str(item.get("component") or "").strip()
            if not comp:
                continue
            aux = {
                "kpi": str(item.get("kpi") or "").strip(),
                "time": str(item.get("time") or "").strip(),
                "change": str(item.get("change") or "").strip(),
                "duration_approx": str(item.get("duration_approx") or "").strip(),
                "anomaly_value": str(item.get("anomaly_value") or "").strip(),
                "anomaly_type": str(item.get("anomaly_type") or "").strip(),
                "drop_reason": str(item.get("drop_reason") or "").strip(),
            }
            bucket = self._filtered_localize_aux.setdefault(comp, [])
            key = (
                aux["kpi"],
                aux["time"],
                aux["change"],
                aux["duration_approx"],
                aux["anomaly_value"],
                aux["drop_reason"],
            )
            seen = {
                (
                    str(x.get("kpi") or "").strip(),
                    str(x.get("time") or "").strip(),
                    str(x.get("change") or "").strip(),
                    str(x.get("duration_approx") or "").strip(),
                    str(x.get("anomaly_value") or "").strip(),
                    str(x.get("drop_reason") or "").strip(),
                )
                for x in bucket
            }
            if key not in seen:
                bucket.append(aux)

    def _get_filtered_localize_aux_for_node(
        self, component: str, node_time: str | None, window_min: int = 5
    ) -> list[dict]:
        """Return filtered localization aux evidence for this node/component near node_time."""
        comp = str(component or "").strip()
        if not comp:
            return []
        items = list(self._filtered_localize_aux.get(comp, []))
        if not items:
            return []
        if not node_time:
            return items[:8]
        near: list[dict] = []
        for item in items:
            t = str(item.get("time") or "").strip()
            if self._is_time_within_minutes(node_time, t, window_min):
                near.append(item)
        return (near or items)[:8]

    def _record_localize_fallback_items(
        self,
        items: list[dict],
        *,
        source: str,
        filtered: bool,
    ) -> None:
        """Store localization findings for zero-candidate fallback ranking."""
        if not items:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            comp = str(item.get("component") or "").strip()
            if not comp:
                continue
            try:
                severity = float(item.get("severity", 0.0) or 0.0)
            except (TypeError, ValueError):
                severity = 0.0
            value_is_problematic = item.get("value_is_problematic")
            if isinstance(value_is_problematic, str):
                value_is_problematic = value_is_problematic.strip().lower() in {
                    "true", "yes", "1", "problematic"
                }
            normalized_time = self._normalize_outlier_time(
                str(item.get("time") or "").strip()
            ) or str(item.get("time") or "").strip()
            payload = {
                "component": comp,
                "time": normalized_time,
                "reason_hint": str(item.get("reason_hint") or "").strip(),
                "kpi": str(item.get("kpi") or "").strip(),
                "change": str(item.get("change") or "").strip(),
                "duration_approx": str(item.get("duration_approx") or "").strip(),
                "anomaly_value": str(item.get("anomaly_value") or "").strip(),
                "anomaly_type": str(item.get("anomaly_type") or "").strip(),
                "severity": severity,
                "value_is_problematic": value_is_problematic,
                "value_judgment": str(item.get("value_judgment") or "").strip(),
                "drop_reason": str(item.get("drop_reason") or "").strip(),
                "source": source,
                "filtered": bool(filtered),
            }
            key = (
                payload["component"],
                payload["time"],
                payload["kpi"],
                payload["change"],
                payload["source"],
                payload["filtered"],
                payload["drop_reason"],
            )
            seen = {
                (
                    str(x.get("component") or "").strip(),
                    str(x.get("time") or "").strip(),
                    str(x.get("kpi") or "").strip(),
                    str(x.get("change") or "").strip(),
                    str(x.get("source") or "").strip(),
                    bool(x.get("filtered")),
                    str(x.get("drop_reason") or "").strip(),
                )
                for x in self._localize_fallback_pool
            }
            if key not in seen:
                self._localize_fallback_pool.append(payload)

    # ── Expand-first flow: build dependency tree then deep dive leaf→root ─

    def _run_full_expand(self, candidate_ids: list[str], max_expand_iters: int = 15) -> None:
        """Build anomaly dependency tree before deep dive.

        Preferred path: use `_stage3_controller_expand` so LLM sees full topology
        dependencies and keeps only anomaly-bearing nodes.
        Fallback path: graph-only expand when controller is unavailable.
        """
        if not candidate_ids:
            return

        queue: deque[str] = deque(candidate_ids)
        in_tree_components = {
            self.tree.nodes[nid].component for nid in self.tree.nodes if nid != "root"
        }
        iteration = 0

        while queue and iteration < max_expand_iters:
            iteration += 1
            node_id = queue.popleft()
            node = self.tree.nodes.get(node_id)
            if not node:
                continue

            added = 0

            if self.use_controller_expand and self.problem:
                # LLM-driven expand: dependency candidates from topology, then anomaly filter.
                new_ids = self._stage3_controller_expand([node_id], [])
                for nid in new_ids:
                    n = self.tree.nodes.get(nid)
                    if not n:
                        continue
                    comp = (n.component or "").strip()
                    if not comp or comp in in_tree_components:
                        continue
                    in_tree_components.add(comp)
                    queue.append(nid)
                    added += 1
            else:
                # Fallback: graph-only expand without LLM filtering.
                graph_related = get_related_components_for_expand(
                    node.component, self.profile.name or ""
                )
                for comp in graph_related:
                    if not comp or comp in in_tree_components:
                        continue
                    in_tree_components.add(comp)
                    new_id = self.tree.add_candidate(
                        stage="expand",
                        component=comp,
                        parent_id=node_id,
                        time_str=node.time,
                        relation="graph",
                    )
                    queue.append(new_id)
                    added += 1
                    self._log_service(f"[Expand (graph)] + {comp} under {node.component}")

            self._update_live_view()
            if added:
                logger.info(f"Full expand iter {iteration}: +{added} from {node.component}")

        self._log_service(
            f"Full expand done: {len(in_tree_components)} components in tree "
            f"(excluding root) after {iteration} iterations."
        )

    def _run_single_expand_pass(self, candidate_ids: list[str]) -> None:
        """Depth-limited expand: localization candidates (1-hop) and their children (2-hop)."""
        if not candidate_ids:
            return

        total_added = 0
        first_hop_new: list[str] = []

        def _expand_from(node_ids: list[str]) -> list[str]:
            nonlocal total_added
            new_ids: list[str] = []
            for node_id in node_ids:
                node = self.tree.nodes.get(node_id)
                if not node:
                    continue
                added = 0
                if self.use_controller_expand and self.problem:
                    batch_new = self._stage3_controller_expand([node_id], [])
                    added = len(batch_new)
                    new_ids.extend(batch_new)
                else:
                    graph_related = get_related_components_for_expand(
                        node.component, self.profile.name or ""
                    )
                    existing_children_under_parent = {
                        (
                            (n.parent_id or "").strip(),
                            (n.component or "").strip(),
                        )
                        for n in self.tree.nodes.values()
                        if (n.component or "").strip()
                    }
                    for comp in graph_related:
                        if not comp or (node_id, comp) in existing_children_under_parent:
                            continue
                        cid = self.tree.add_candidate(
                            stage="expand",
                            component=comp,
                            parent_id=node_id,
                            time_str=node.time,
                            relation="graph",
                        )
                        new_ids.append(cid)
                        added += 1
                        self._log_service(f"[Expand (single-pass graph)] + {comp} under {node.component}")
                total_added += added
                if added:
                    self._update_live_view()
                    logger.info(f"Single-pass expand: +{added} from {node.component}")
            return new_ids

        # 1-hop: expand directly from localization candidates (e.g., os_020 → docker_004/docker_008).
        first_hop_new = _expand_from(candidate_ids)

        # 2-hop (optional): expand once more from the newly added expand nodes (e.g., docker_008 → DBs).
        if self.expand_max_hops >= 2 and first_hop_new:
            _expand_from(first_hop_new)

        hop_label = "1-hop" if self.expand_max_hops < 2 else "2-hop"
        self._log_service(
            f"Depth-limited expand ({hop_label}) done: +{total_added} expand nodes from "
            f"{len(candidate_ids)} localization candidates."
        )

    def _get_leaf_to_root_order(self) -> list[str]:
        """Return node ids from leaves (deepest) to root (nearest to root), so we
        deep-dive leaves first then work upward to find root cause.
        """
        if not self.tree.nodes:
            return []
        depth: dict[str, int] = {"root": 0}
        queue: deque[str] = deque(["root"])
        while queue:
            nid = queue.popleft()
            d = depth[nid]
            node = self.tree.nodes.get(nid)
            if not node:
                continue
            for cid in node.children:
                if cid not in depth:
                    depth[cid] = d + 1
                    queue.append(cid)
        all_ids = [nid for nid in self.tree.nodes if nid != "root"]
        all_ids.sort(key=lambda x: -depth.get(x, 0))
        return all_ids

    def _run_leaf_to_root_deep_dive(self, ordered_node_ids: list[str]) -> None:
        """Run deep dive on each node in leaf→root order. Updates tree (confirm/prune)."""
        for i, node_id in enumerate(ordered_node_ids):
            node = self.tree.nodes.get(node_id)
            if not node or node.status == "pruned":
                continue
            logger.info(f"Stage 2: Deep Dive ({i + 1}/{len(ordered_node_ids)}) — {node.component}")
            self._log_agent(
                f"=== Stage 2: Deep Dive (leaf→root {i + 1}/{len(ordered_node_ids)}): {node.component} ===",
                step_boundary=True,
            )
            if self.use_controller_deep_dive and self.problem:
                hypothesis_ids = self._stage2_controller_deep_dive([node_id])
            else:
                hypothesis_ids = self.stage2_deep_dive([node_id])
            if hypothesis_ids:
                self._log_service(
                    f"  → hypotheses: "
                    + ", ".join(
                        f"{self.tree.nodes[h].component}/{self.tree.nodes[h].reason}(conf={self.tree.nodes[h].confidence:.2f})"
                        for h in hypothesis_ids
                    )
                )
            self._update_live_view()

    def _build_global_judge_tree_summary(self) -> str:
        """Serialize the current non-root tree into a compact JSON summary."""
        def _json_safe(value):
            if isinstance(value, datetime):
                return value.strftime("%Y-%m-%d %H:%M:%S")
            if isinstance(value, dict):
                return {str(k): _json_safe(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [_json_safe(v) for v in value]
            return value

        nodes_payload: list[dict] = []
        for nid, node in self.tree.nodes.items():
            if nid == "root" or node.status == "pruned":
                continue
            parent = self.tree.nodes.get(node.parent_id or "")
            evidence = (getattr(node, "evidence", "") or "").replace("\n", " ").strip()
            if len(evidence) > 280:
                evidence = evidence[:277] + "..."
            nodes_payload.append(
                {
                    "id": nid,
                    "stage": node.stage,
                    "status": node.status,
                    "component": node.component,
                    "parent_id": node.parent_id,
                    "parent_component": parent.component if parent else "",
                    "children": list(node.children),
                    "time": node.time,
                    "reason_hint": node.reason,
                    "root_cause_reason_class": getattr(node, "root_cause_reason_class", None),
                    "kpi": getattr(node, "kpi", None),
                    "relation": getattr(node, "relation", None),
                    "confidence": float(getattr(node, "confidence", 0.0) or 0.0),
                    "severity": float(getattr(node, "severity", 0.0) or 0.0),
                    "localized_match": bool(getattr(node, "localized_match", False)),
                    "localized_time": getattr(node, "localized_time", None),
                    "localized_severity": float(getattr(node, "localized_severity", 0.0) or 0.0),
                    "deep_dive_reason": getattr(node, "deep_dive_reason", None),
                    "deep_dive_reason_class": getattr(node, "deep_dive_reason_class", None),
                    "deep_dive_confidence": float(getattr(node, "deep_dive_confidence", 0.0) or 0.0),
                    "deep_dive_time": getattr(node, "deep_dive_time", None),
                    "deep_dive_evidence": (getattr(node, "deep_dive_evidence", "") or "")[:600],
                    "deep_dive_checked_reasons": list(getattr(node, "deep_dive_checked_reasons", []) or [])[:8],
                    "evidence": evidence,
                    "filtered_localize_aux": self._get_filtered_localize_aux_for_node(
                        node.component,
                        node.time,
                        window_min=5,
                    ),
                }
            )

        included_components = {
            str(item.get("component") or "").strip()
            for item in nodes_payload
            if str(item.get("component") or "").strip()
        }
        trace_edge_counts: dict[tuple[str, str], int] = {}
        for edge in self.tree.trace_edges:
            if not isinstance(edge, dict):
                continue
            src = str(edge.get("from") or "").strip()
            dst = str(edge.get("to") or "").strip()
            if not src or not dst:
                continue
            # Keep only edges relevant to components still present in the summarized tree.
            if src not in included_components and dst not in included_components:
                continue
            trace_edge_counts[(src, dst)] = trace_edge_counts.get((src, dst), 0) + 1
        trace_edges_payload = [
            {"from": src, "to": dst, "count": count}
            for (src, dst), count in sorted(
                trace_edge_counts.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1]),
            )[:100]
        ]
        included_node_ids = {item["id"] for item in nodes_payload}
        relation_edges_payload: list[dict] = []
        for edge in self.tree.relation_edges:
            if not isinstance(edge, dict):
                continue
            src_node_id = str(edge.get("src_node_id") or "").strip()
            dst_node_id = str(edge.get("dst_node_id") or "").strip()
            src_component = str(edge.get("src_component") or "").strip()
            dst_component = str(edge.get("dst_component") or "").strip()
            if src_node_id and src_node_id not in included_node_ids:
                continue
            if dst_node_id and dst_node_id not in included_node_ids:
                continue
            if (
                src_component and src_component not in included_components
                and dst_component and dst_component not in included_components
            ):
                continue
            relation_edges_payload.append(
                {
                    "src_node_id": src_node_id or None,
                    "dst_node_id": dst_node_id or None,
                    "src_component": src_component or None,
                    "dst_component": dst_component or None,
                    "relation_family": edge.get("relation_family"),
                    "relation_type": edge.get("relation_type"),
                    "anomaly_type": edge.get("anomaly_type"),
                    "time": edge.get("time"),
                    "label": edge.get("label"),
                    "metadata": edge.get("metadata") or {},
                }
            )
        containment_groups_payload: list[dict] = []
        for group in self.tree.containment_groups:
            if not isinstance(group, dict):
                continue
            member_ids = [
                str(mid).strip()
                for mid in (group.get("member_node_ids") or [])
                if str(mid).strip() in included_node_ids
            ]
            if len(member_ids) < 2:
                continue
            containment_groups_payload.append(
                {
                    "container_node_id": group.get("container_node_id"),
                    "container_component": group.get("container_component"),
                    "member_node_ids": member_ids,
                    "relation_family": group.get("relation_family"),
                    "relation_type": group.get("relation_type"),
                    "label": group.get("label"),
                    "metadata": group.get("metadata") or {},
                }
            )

        leaf_ids = [
            nid for nid, node in self.tree.nodes.items()
            if nid != "root" and node.status != "pruned" and not node.children
        ]
        paths_payload: list[dict] = []
        for leaf_id in leaf_ids:
            chain: list[str] = []
            cur = self.tree.nodes.get(leaf_id)
            guard = 0
            while cur and guard < 64:
                chain.append(cur.id)
                if cur.parent_id == "root":
                    chain.append("root")
                    break
                cur = self.tree.nodes.get(cur.parent_id or "")
                guard += 1
            chain.reverse()
            component_path = [
                self.tree.nodes[nid].component if nid in self.tree.nodes else "System Failure"
                for nid in chain
            ]
            paths_payload.append(
                {
                    "leaf_id": leaf_id,
                    "node_path": chain,
                    "component_path": component_path,
                }
            )

        payload = {
            "nodes": nodes_payload,
            "paths": paths_payload,
            "containment_groups": containment_groups_payload,
            "relation_edges": relation_edges_payload,
            "trace_edges": trace_edges_payload,
            "query_window": _json_safe(self.time_range),
        }
        return json.dumps(_json_safe(payload), ensure_ascii=False, indent=2)

    def _resolve_global_judge_node(self, verdict: dict) -> TreeNode | None:
        """Resolve the chosen tree node from a global-judge verdict."""
        node_id = (verdict.get("node_id") or "").strip()
        if node_id and node_id in self.tree.nodes and node_id != "root":
            return self.tree.nodes[node_id]

        component = (verdict.get("component") or "").strip()
        if not component:
            return None

        candidates = [
            node for nid, node in self.tree.nodes.items()
            if nid != "root" and node.status != "pruned" and node.component == component
        ]
        if not candidates:
            return None

        target_time = self._normalize_outlier_time((verdict.get("time") or "").strip())
        if target_time:
            try:
                target_dt = datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S")
                candidates.sort(
                    key=lambda n: (
                        abs(
                            (
                                datetime.strptime(n.time, "%Y-%m-%d %H:%M:%S") - target_dt
                            ).total_seconds()
                        ) if (n.time or "").strip() else float("inf"),
                        -(float(getattr(n, "deep_dive_confidence", 0.0) or 0.0)),
                        -(float(getattr(n, "confidence", 0.0) or 0.0)),
                        -(float(getattr(n, "severity", 0.0) or 0.0)),
                    )
                )
            except ValueError:
                pass
        else:
            candidates.sort(
                key=lambda n: (
                    -(float(getattr(n, "deep_dive_confidence", 0.0) or 0.0)),
                    -(float(getattr(n, "confidence", 0.0) or 0.0)),
                    -(float(getattr(n, "severity", 0.0) or 0.0)),
                )
            )
        return candidates[0] if candidates else None

    def _fallback_shortlist_node_ids(self, top_k: int) -> list[str]:
        """Heuristic shortlist when LLM pre-judge is unavailable/invalid."""
        scored: list[tuple[tuple[float, float, float, float, float], str]] = []
        for nid, node in self.tree.nodes.items():
            if nid == "root" or node.status == "pruned":
                continue
            deep_conf = float(getattr(node, "deep_dive_confidence", 0.0) or 0.0)
            stage_score = 1.0 if node.stage == "deep_dive" else (0.85 if deep_conf > 0 else (0.7 if node.stage == "expand" else 0.4))
            conf = float(getattr(node, "confidence", 0.0) or 0.0)
            sev = float(getattr(node, "severity", 0.0) or 0.0) / 100.0
            localized = 1.0 if getattr(node, "localized_match", False) else 0.0
            scored.append(((stage_score, deep_conf, conf, sev, localized), nid))
        scored.sort(reverse=True, key=lambda x: x[0])
        out: list[str] = []
        seen_components: set[str] = set()
        for _, nid in scored:
            node = self.tree.nodes[nid]
            comp = str(node.component or "").strip()
            if comp in seen_components and len(out) < top_k:
                # allow duplicates only if we still don't have enough
                pass
            else:
                seen_components.add(comp)
            out.append(nid)
            if len(out) >= top_k:
                break
        return out

    def stage4_global_shortlist(self, top_k: int = 3) -> list[str]:
        """Global pre-judge: shortlist top-k candidates for focused deep dive."""
        top_k = max(1, int(top_k))
        tree_summary = self._build_global_judge_tree_summary()
        summary_payload = json.loads(tree_summary)
        if not summary_payload.get("nodes"):
            self._log_service("[Global Shortlist] Skipped: tree is empty.")
            return []

        system = _GLOBAL_SHORTLIST_SYSTEM_TEMPLATE.format(top_k=top_k)
        user_message = (
            f"Dataset profile: {self.profile.name!r}. Namespace: {self.namespace!r}. "
            f"Return at most Top-{top_k} candidate nodes for deep-dive verification.\n\n"
            f"{tree_summary}"
        )
        raw = get_chat_completion(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            self.configs,
            temperature=0.0,
        )
        self._log_service(f"[Global Shortlist] Raw response: {raw}")
        shortlist: list[str] = []
        try:
            payload = json.loads(self._extract_json(raw))
            candidates = payload.get("candidates", [])
            if isinstance(candidates, list):
                for item in candidates:
                    if not isinstance(item, dict):
                        continue
                    chosen = self._resolve_global_judge_node(item)
                    if not chosen:
                        continue
                    if chosen.id not in shortlist:
                        shortlist.append(chosen.id)
                    if len(shortlist) >= top_k:
                        break
        except Exception as e:
            self._log_service(f"[Global Shortlist] Failed to parse response: {e}")

        if not shortlist:
            shortlist = self._fallback_shortlist_node_ids(top_k=top_k)
            self._log_service(
                f"[Global Shortlist] Fallback shortlist used: {shortlist}"
            )
        else:
            self._log_service(
                f"[Global Shortlist] Selected candidate node_ids: {shortlist}"
            )
        return shortlist

    def stage4_shortlist_deep_dive(self, shortlist_ids: list[str]) -> list[str]:
        """Run deep dive for each shortlisted candidate and return confirmed deep-dive node ids."""
        confirmed_ids: list[str] = []
        if not shortlist_ids:
            return confirmed_ids
        for idx, nid in enumerate(shortlist_ids):
            node = self.tree.nodes.get(nid)
            if not node:
                continue
            self._log_service(
                f"[Shortlist Deep Dive] ({idx + 1}/{len(shortlist_ids)}) "
                f"component={node.component!r}, node_id={nid}"
            )
            if self.use_controller_deep_dive and self.problem:
                out_ids = self._stage2_controller_deep_dive([nid])
            else:
                out_ids = self.stage2_deep_dive([nid])
            for out_id in out_ids:
                out_node = self.tree.nodes.get(out_id)
                if (
                    out_node
                    and (
                        out_node.status == "confirmed"
                        or float(getattr(out_node, "deep_dive_confidence", 0.0) or 0.0) >= 0.5
                    )
                    and out_id not in confirmed_ids
                ):
                    confirmed_ids.append(out_id)
        if confirmed_ids:
            self._log_service(f"[Shortlist Deep Dive] Confirmed deep-dive nodes: {confirmed_ids}")
        else:
            self._log_service("[Shortlist Deep Dive] No shortlist candidate survived deep dive.")
        return confirmed_ids

    def _resolve_follow_up_node_ids(self, follow_ups: list, limit: int = 3) -> list[str]:
        """Resolve follow-up candidate entries (node_id/component/time) into concrete node ids."""
        resolved: list[str] = []
        if not isinstance(follow_ups, list):
            return resolved
        for item in follow_ups:
            if len(resolved) >= limit:
                break
            if not isinstance(item, dict):
                continue
            node = self._resolve_global_judge_node(item)
            if node and node.id not in resolved:
                resolved.append(node.id)
        return resolved

    def stage4_global_judge(self) -> TreeNode | None:
        """Pick final root cause; if judge asks for more investigation, loop back to deep dive."""
        max_retries = 2
        possible_reasons = list(self.profile.possible_reasons or [])
        possible_reasons_list = "\n".join(
            f"- {r}" for r in possible_reasons
        ) if possible_reasons else "(none — return the best-supported reason string)"
        system = _GLOBAL_JUDGE_SYSTEM_TEMPLATE.format(
            possible_reasons_list=possible_reasons_list,
        )

        for attempt in range(max_retries + 1):
            tree_summary = self._build_global_judge_tree_summary()
            summary_payload = json.loads(tree_summary)
            if not summary_payload.get("nodes"):
                self._log_service("[Global Judge] Skipped: tree is empty.")
                return None

            out_path = self.save_dir / "global_judge_tree_summary.json"
            try:
                out_path.write_text(tree_summary, encoding="utf-8")
                self._log_service(f"[Global Judge] Tree summary saved to {out_path}")
            except Exception as e:
                self._log_service(f"[Global Judge] Failed to save tree summary: {e}")

            user_message = (
                f"Dataset profile: {self.profile.name!r}. Namespace: {self.namespace!r}. "
                "Below is the full RCA search tree summary as JSON. "
                "Choose final root cause, or request additional deep dive on 1-3 candidates if needed.\n\n"
                f"{tree_summary}"
            )
            raw = get_chat_completion(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
                self.configs,
                temperature=0.0,
            )
            self._log_service(f"[Global Judge] Raw response (attempt {attempt + 1}): {raw}")
            try:
                verdict = json.loads(self._extract_json(raw))
            except Exception as e:
                self._log_service(f"[Global Judge] Failed to parse response: {e}")
                return None

            decision = str(verdict.get("decision") or "final").strip().lower()
            if decision == "investigate_more" and attempt < max_retries:
                follow_ups = verdict.get("follow_up_candidates")
                follow_ids = self._resolve_follow_up_node_ids(follow_ups, limit=3)
                if not follow_ids:
                    # fallback: strongest nodes if follow-up parse fails
                    follow_ids = self._fallback_shortlist_node_ids(top_k=3)
                self._log_service(
                    f"[Global Judge] Requested extra deep dive on nodes: {follow_ids}"
                )
                if not follow_ids:
                    self._log_service("[Global Judge] No valid follow-up candidates; finalizing with current tree.")
                    continue
                self.stage4_shortlist_deep_dive(follow_ids)
                self._update_live_view()
                continue
            if decision == "investigate_more" and attempt >= max_retries:
                # Forced finalization at retry limit: pick the strongest candidate now.
                follow_ups = verdict.get("follow_up_candidates")
                forced_ids = self._resolve_follow_up_node_ids(follow_ups, limit=3)
                if not forced_ids:
                    forced_ids = self._fallback_shortlist_node_ids(top_k=3)
                forced_node = None
                if forced_ids:
                    forced_candidates = [
                        self.tree.nodes[nid] for nid in forced_ids if nid in self.tree.nodes
                    ]
                    if forced_candidates:
                        forced_candidates.sort(
                            key=lambda n: (
                                float(getattr(n, "deep_dive_confidence", 0.0) or 0.0),
                                float(getattr(n, "confidence", 0.0) or 0.0),
                                float(getattr(n, "severity", 0.0) or 0.0),
                                1.0 if n.stage == "deep_dive" else 0.0,
                            ),
                            reverse=True,
                        )
                        forced_node = forced_candidates[0]
                if forced_node is None:
                    forced_node = self.tree.get_best_or_highest_confidence()
                if forced_node is None:
                    self._log_service("[Global Judge] Forced finalization failed: no candidate node.")
                    return None
                self._log_service(
                    f"[Global Judge] Retry limit reached; forcing final conclusion with node={forced_node.id!r}, "
                    f"component={forced_node.component!r}"
                )
                verdict = {
                    "decision": "final",
                    "node_id": forced_node.id,
                    "component": forced_node.component,
                    "reason": (
                        getattr(forced_node, "root_cause_reason_class", None)
                        or forced_node.reason
                        or ""
                    ),
                    "time": forced_node.time or "",
                    "confidence": max(0.55, float(getattr(forced_node, "confidence", 0.0) or 0.0)),
                    "explanation": (
                        str(verdict.get("explanation") or "").strip()
                        or "Forced finalization at global judge retry limit."
                    ),
                    "why_not_others": "Retry limit reached; selected strongest remaining candidate.",
                    "supporting_path": verdict.get("supporting_path") if isinstance(verdict.get("supporting_path"), list) else [],
                }

            chosen = self._resolve_global_judge_node(verdict)
            if not chosen:
                self._log_service(
                    f"[Global Judge] Could not resolve chosen node from verdict: {verdict}"
                )
                return None

            conf_raw = verdict.get("confidence", 0.0)
            try:
                confidence = float(conf_raw)
            except (TypeError, ValueError):
                confidence = 0.0
            confidence = max(0.0, min(1.0, confidence))

            reason_raw = verdict.get("reason")
            reason_class = self._normalize_reason_to_class(
                reason_raw or chosen.reason or getattr(chosen, "root_cause_reason_class", None)
            )
            if not reason_class and self.profile.possible_reasons:
                self._log_service(
                    f"[Global Judge] Rejected invalid reason {reason_raw!r}; allowed={self.profile.possible_reasons}"
                )
                return None

            new_time_raw = (verdict.get("time") or "").strip()
            normalized_time = self._normalize_outlier_time(new_time_raw) if new_time_raw else None
            if normalized_time:
                chosen.time = normalized_time

            explanation = str(verdict.get("explanation", "")).strip()
            why_not_others = str(verdict.get("why_not_others", "")).strip()
            support = verdict.get("supporting_path")
            support_text = ""
            if isinstance(support, list) and support:
                support_text = "supporting_path=" + " -> ".join(str(x) for x in support[:12])
            final_evidence = " | ".join(
                part for part in (explanation, why_not_others, support_text) if part
            )

            chosen.reason = reason_class or chosen.reason
            chosen.root_cause_reason_class = (
                reason_class or getattr(chosen, "root_cause_reason_class", None)
            )
            self.tree.confirm(
                chosen.id,
                confidence=confidence,
                evidence=final_evidence,
                reason=chosen.reason,
                root_cause_reason_class=chosen.root_cause_reason_class,
            )
            self._update_live_view()
            return chosen

        return None

    # ── main entry point ─────────────────────────────────────────────

    def run(self, max_iterations: int = 20) -> dict:
        """Run the staged pipeline, return prediction dict."""

        # ── Stage 1: Localization ─────────────────────────────────────
        logger.info("Stage 1: Localization")
        self._log_agent("=== Stage 1: Localization ===", step_boundary=True)
        candidate_ids = self.stage1_localize()

        candidate_summary = ", ".join(
            f"{self.tree.nodes[c].component}({self.tree.nodes[c].reason})"
            for c in candidate_ids
        )
        logger.info(f"Stage 1 → {len(candidate_ids)} candidates")
        self._log_service(
            f"Stage 1 found {len(candidate_ids)} candidates:\n{candidate_summary}"
        )
        self._update_live_view()

        # Precompute trace caller→callee edges between localized components
        self._precompute_trace_edges(candidate_ids)
        self._save_trace_anomaly_edges()

        # Sort candidates by severity (highest first) so we traverse worst anomalies first
        candidate_ids = self._sort_candidates_by_priority(candidate_ids)

        # ── Save tree (so far), build host containment groups, render ──
        tree_path = self.save_dir / "tree.json"
        self.tree.save(tree_path)
        if candidate_ids:
            self._build_localization_host_containment_groups(candidate_ids)
            self._render_localization_dependency_graph(candidate_ids)
        if self.render_localization_timeline and candidate_ids:
            self._render_localization_timeline(tree_path)

        # ── Stage 3: single expand pass only from localization candidates ──
        if self.enable_expand and candidate_ids:
            logger.info("Stage 3: Single-pass Expand from localization candidates")
            self._log_agent(
                "=== Stage 3: Single-pass Expand from localization candidates ===",
                step_boundary=True,
            )
            self._run_single_expand_pass(candidate_ids)
            self.tree.save(tree_path)
            self._update_live_view()

        # ── Stage 2: optional deep dive from leaf → root ──────────────
        if self.enable_deep_dive:
            ordered_ids = self._get_leaf_to_root_order()
            if not ordered_ids:
                ordered_ids = candidate_ids
            logger.info(f"Stage 2: Deep Dive leaf→root ({len(ordered_ids)} nodes)")
            self._log_agent(
                f"=== Stage 2: Deep Dive leaf→root ({len(ordered_ids)} nodes) ===",
                step_boundary=True,
            )
            self._run_leaf_to_root_deep_dive(ordered_ids)
        else:
            logger.info("Stage 2: Deep Dive skipped (enable_deep_dive=False)")
            self._log_agent(
                "=== Stage 2: Deep Dive skipped (enable_deep_dive=False) ===",
                step_boundary=True,
            )

        # ── Stage 4: global shortlist (Top-K) + focused deep dive ────
        logger.info("Stage 4: Global Shortlist (Top-3) + Deep Dive")
        self._log_agent(
            "=== Stage 4: Global Shortlist (Top-3) + Focused Deep Dive ===",
            step_boundary=True,
        )
        shortlist_ids = self.stage4_global_shortlist(top_k=3)
        if shortlist_ids:
            self.stage4_shortlist_deep_dive(shortlist_ids)
            self.tree.save(tree_path)
            self._update_live_view()

        # ── Stage 5: final global judge over the whole search tree ────
        logger.info("Stage 5: Global Judge")
        self._log_agent("=== Stage 5: Global Judge ===", step_boundary=True)
        judged = self.stage4_global_judge()
        if judged:
            self._log_service(
                f"[Global Judge] Selected node={judged.id!r}, component={judged.component!r}, "
                f"reason={getattr(judged, 'root_cause_reason_class', '')!r}, time={judged.time!r}, "
                f"confidence={judged.confidence:.2f}"
            )

        # ── Save tree ─────────────────────────────────────────────────
        tree_path = self.save_dir / "tree.json"
        self.tree.save(tree_path)
        logger.info(f"Search tree saved to {tree_path}")

        best = judged or self.tree.get_best_or_highest_confidence()
        if best:
            # After deep dive: always return time, component, reason; reason normalized to allowed class
            reason_class = (
                getattr(best, "root_cause_reason_class", None) or ""
            ).strip() or self._normalize_reason_to_class(best.reason)
            if not self._is_valid_prediction(best):
                self._log_service(
                    f"Returning best node anyway (validation failed): "
                    f"component={best.component!r}, reason_class={reason_class!r}"
                )
            return {
                "component": best.component or "",
                "reason": reason_class,
                "datetime": best.time or "",
            }
        return {"component": "", "reason": "", "datetime": ""}

    # ── Stage 1: Localization ────────────────────────────────────────

    def stage1_localize(self) -> list[str]:
        """Run metric-based localization only and collect vision critic outliers."""
        candidate_ids: list[str] = []

        def _find_cluster_node(
            component: str,
            time_str: str | None,
            window_min: float = 5.0,
        ):
            """Find existing localization node for same component within ±window_min."""
            if not component or not time_str:
                return None
            try:
                t_new = datetime.strptime(str(time_str).strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
            best_match = None
            best_key = None
            for n in self.tree.nodes.values():
                if n.stage != "localize" or n.component != component or not (n.time or "").strip():
                    continue
                try:
                    t_old = datetime.strptime(str(n.time).strip(), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                delta_sec = abs((t_new - t_old).total_seconds())
                if delta_sec > window_min * 60.0:
                    continue
                # Prefer higher severity, then closer in time
                key = (
                    -float(getattr(n, "severity", 0.0) or 0.0),
                    delta_sec,
                )
                if best_key is None or key < best_key:
                    best_key = key
                    best_match = n
            return best_match

        def _canonicalize_localize_component(component: str | None) -> str:
            comp = (component or "").strip()
            if not comp:
                return ""

            dataset = (self.profile.name or "").replace("openrca_", "")
            if dataset.startswith("market"):
                leaf = comp.split(".", 1)[-1]
                explicit_levels = self.profile.component_levels or {}
                if leaf in set(explicit_levels.get("pod", []) or []):
                    return leaf
                if leaf in set(explicit_levels.get("service", []) or []):
                    return leaf
                if leaf in set(explicit_levels.get("node", []) or []):
                    return leaf

            if self._get_level(comp):
                if comp in {
                    c
                    for comps in self.profile.component_levels.values()
                    for c in comps
                }:
                    return comp

            return comp

        # 1) SR drop graph → narrow time window
        try:
            self._log_agent("[Localize] Scanning SR drop graph")
            sr_result = self.actions.get_success_rate_drop_graph(
                self.namespace,
            )
            sr_images = self._extract_image_paths(sr_result)
            if sr_images:
                sr_text = self._extract_visual_summary_text(sr_result)
                sr_prompt = self._critic_prompt
                if sr_text:
                    sr_prompt = (
                        sr_prompt
                        + "\n\nStructured anomaly hints from renderer text (use to disambiguate colors/overlap):\n"
                        + sr_text
                        + "\nIf image and text appear inconsistent, trust numeric/metric text."
                    )
                sr_analysis = self._call_vision_critic(
                    sr_images,
                    context="SR drop",
                    prompt_override=sr_prompt,
                )
                self._log_service(f"[SR drop] Vision critic:\n{sr_analysis}")
                sr_outliers, sr_filtered_aux = self._parse_outliers(
                    sr_analysis,
                    "success_rate",
                    include_filtered_aux=True,
                )
                self._store_filtered_localize_aux(sr_filtered_aux)
                self._record_localize_fallback_items(
                    sr_outliers,
                    source="sr_drop/success_rate",
                    filtered=False,
                )
                self._record_localize_fallback_items(
                    sr_filtered_aux,
                    source="sr_drop/success_rate",
                    filtered=True,
                )
                if sr_outliers:
                    self._log_service(
                        f"[SR drop] Found {len(sr_outliers)} outlier(s): "
                        + ", ".join(
                            f"{_canonicalize_localize_component(o.get('component'))}({o.get('reason_hint','')})"
                            for o in sr_outliers
                        )
                    )
                for o in sr_outliers:
                    comp = _canonicalize_localize_component(o.get("component"))
                    if not comp:
                        continue
                    t_str = o.get("time")
                    kpi_name = o.get("kpi") or "success_rate"
                    severity = float(o.get("severity", 0.0))
                    evidence_parts = [o.get("change", "success-rate drop")]
                    for key in ("duration_approx", "anomaly_value", "anomaly_type", "value_judgment"):
                        if o.get(key):
                            evidence_parts.append(f"{key}: {o[key]}")
                    evidence = " | ".join(str(x) for x in evidence_parts if x)

                    cluster_node = _find_cluster_node(comp, t_str, window_min=5.0)
                    if cluster_node is not None:
                        try:
                            prev_sev = float(getattr(cluster_node, "severity", 0.0) or 0.0)
                        except (TypeError, ValueError):
                            prev_sev = 0.0
                        if severity > prev_sev:
                            cluster_node.severity = severity
                        existing_kpi = getattr(cluster_node, "kpi", []) or []
                        if isinstance(existing_kpi, str):
                            kpi_list = [k.strip() for k in existing_kpi.split(",") if k.strip()]
                        else:
                            kpi_list = list(existing_kpi)
                        if kpi_name not in kpi_list:
                            kpi_list.append(kpi_name)
                        cluster_node.kpi = kpi_list
                        prev_evi = (cluster_node.evidence or "").strip()
                        cluster_node.evidence = prev_evi + " || " + evidence if prev_evi else evidence
                        self._log_service(
                            f"[Localize] Merged SR KPI {kpi_name!r} for component {comp!r} "
                            f"into existing 5-min cluster at {cluster_node.time!r}."
                        )
                        continue

                    nid = self.tree.add_candidate(
                        stage="localize",
                        component=comp,
                        reason=o.get("reason_hint"),
                        time_str=t_str,
                        kpi=[kpi_name],
                        evidence=evidence,
                        severity=severity,
                    )
                    candidate_ids.append(nid)
                    self._update_live_view()
        except Exception as e:
            self._log_service(f"[SR drop] Failed: {e}")

        # 2) Trace anomalous path graph → multiple root cause candidates
        try:
            self._log_agent("[Localize] Scanning trace anomalous path graph")
            if hasattr(self.actions, "get_trace_anomalous_path_graph"):
                dataset_name = str((self.profile.name or "")).replace("openrca_", "").lower()
                trace_min_edge_volume = 5 if dataset_name.startswith("market") else 20
                path_result = self.actions.get_trace_anomalous_path_graph(
                    self.namespace,
                    window_minutes=30,
                    top_k_paths=5,
                    min_edge_volume=trace_min_edge_volume,
                )
                path_images = self._extract_image_paths(path_result)
                if path_images:
                    deploy_text = self._format_deployment_graph_text()
                    path_text = self._extract_visual_summary_text(path_result)
                    prompt = _TRACE_PATH_VISION_CRITIC
                    if deploy_text:
                        prompt = prompt + "\n\n**Deployment graph** (host → components):\n" + deploy_text
                    if path_text:
                        prompt = (
                            prompt
                            + "\n\n**Structured anomaly hints from renderer text** "
                              "(use this to disambiguate colors/overlap in the image):\n"
                            + path_text
                            + "\nIf image and text conflict, prioritize the numeric text hints."
                        )
                    path_analysis = self._call_vision_critic(
                        path_images,
                        context="trace_anomalous_path",
                        prompt_override=prompt,
                    )
                    self._log_service(
                        f"[Trace anomalous path] Vision critic:\n{path_analysis}"
                    )
                    path_outliers = self._parse_trace_path_outliers(path_analysis)
                    self._record_localize_fallback_items(
                        path_outliers,
                        source="trace_anomalous_path",
                        filtered=False,
                    )
                    if path_outliers:
                        self._log_service(
                            f"[Trace anomalous path] Found {len(path_outliers)} candidate(s): "
                            + ", ".join(
                                f"{o['component']}({o.get('reason_hint','')})"
                                for o in path_outliers
                            )
                        )
                    for o in path_outliers:
                        comp = o["component"]
                        t_str = o.get("time")
                        severity = float(o.get("severity", 0.0))
                        trace_kpi = str(o.get("anomalous_kpi") or "error_rate").strip()
                        evidence_parts = [o.get("change", "trace_path anomaly")]
                        if o.get("anomaly_value"):
                            evidence_parts.append(f"anomaly_value: {o.get('anomaly_value')}")
                        evidence_parts.append(f"trace_kpi: {trace_kpi}")
                        evidence = " | ".join(str(x) for x in evidence_parts if x)
                        cluster_node = _find_cluster_node(comp, t_str, window_min=5.0)
                        if cluster_node is not None:
                            try:
                                prev_sev = float(
                                    getattr(cluster_node, "severity", 0.0) or 0.0
                                )
                            except (TypeError, ValueError):
                                prev_sev = 0.0
                            if severity > prev_sev:
                                cluster_node.severity = severity
                            existing_kpi = getattr(cluster_node, "kpi", []) or []
                            kpi_list = (
                                [k.strip() for k in existing_kpi.split(",") if k.strip()]
                                if isinstance(existing_kpi, str)
                                else list(existing_kpi)
                            )
                            if trace_kpi not in kpi_list:
                                kpi_list.append(trace_kpi)
                            cluster_node.kpi = kpi_list
                            prev_evi = (cluster_node.evidence or "").strip()
                            cluster_node.evidence = (
                                prev_evi + " || " + evidence if prev_evi else evidence
                            )
                            self._log_service(
                                f"[Localize] Merged trace-path KPI {trace_kpi!r} for {comp!r} "
                                f"into existing cluster at {cluster_node.time!r}."
                            )
                            continue
                        nid = self.tree.add_candidate(
                            stage="localize",
                            component=comp,
                            reason=o.get("reason_hint"),
                            time_str=t_str,
                            kpi=[trace_kpi],
                            evidence=evidence,
                            severity=severity,
                        )
                        candidate_ids.append(nid)
                        self._update_live_view()
        except Exception as e:
            self._log_service(f"[Trace anomalous path] Failed: {e}")

        # 4) KPI peer graphs for every comp_type × kpi in profile
        for comp_type, kpis in self.profile.kpis_by_type.items():
            for kpi in kpis:
                self._log_agent(f"[Localize] Scanning {comp_type}/{kpi}")
                outliers = self._scan_kpi(comp_type, kpi)
                if outliers:
                    self._log_service(
                        f"[{comp_type}/{kpi}] Found {len(outliers)} outlier(s): "
                        + ", ".join(
                            f"{o['component']}({o.get('reason_hint','')})"
                            for o in outliers
                        )
                    )
                else:
                    self._log_service(f"[{comp_type}/{kpi}] No outliers")

                for o in outliers:
                    comp = _canonicalize_localize_component(o["component"])
                    if not comp:
                        continue
                    t_str = o.get("time")
                    kpi_name = o.get("kpi") or kpi
                    severity = float(o.get("severity", 0.0))
                    evidence_parts = [o.get("change", "")]
                    for key in ("duration_approx", "kpi", "anomaly_value", "anomaly_type", "value_judgment"):
                        if o.get(key):
                            evidence_parts.append(f"{key}: {o[key]}")
                    evidence = " | ".join(evidence_parts)

                    # Try to merge into an existing 5-minute localization cluster
                    cluster_node = _find_cluster_node(comp, t_str, window_min=5.0)
                    if cluster_node is not None:
                        # Update severity to the max of cluster and new KPI
                        try:
                            prev_sev = float(getattr(cluster_node, "severity", 0.0) or 0.0)
                        except (TypeError, ValueError):
                            prev_sev = 0.0
                        if severity > prev_sev:
                            cluster_node.severity = severity
                        # Merge KPI name into list
                        existing_kpi = getattr(cluster_node, "kpi", []) or []
                        if isinstance(existing_kpi, str):
                            kpi_list = [k.strip() for k in existing_kpi.split(",") if k.strip()]
                        else:
                            kpi_list = list(existing_kpi)
                        if kpi_name not in kpi_list:
                            kpi_list.append(kpi_name)
                        cluster_node.kpi = kpi_list
                        # Append evidence for this KPI
                        prev_evi = (cluster_node.evidence or "").strip()
                        if prev_evi:
                            cluster_node.evidence = prev_evi + " || " + evidence
                        else:
                            cluster_node.evidence = evidence
                        self._log_service(
                            f"[Localize] Merged KPI {kpi_name!r} for component {comp!r} "
                            f"into existing 5-min cluster at {cluster_node.time!r}."
                        )
                        continue

                    nid = self.tree.add_candidate(
                        stage="localize",
                        component=comp,
                        reason=o.get("reason_hint"),
                        time_str=t_str,
                        kpi=[kpi_name],
                        evidence=evidence,
                        severity=severity,
                    )
                    candidate_ids.append(nid)
                    self._update_live_view()

        if not candidate_ids:
            fallback_ids = self._stage1_localize_fallback()
            if fallback_ids:
                candidate_ids.extend(fallback_ids)

        return candidate_ids

    def _stage1_localize_fallback(self, top_k: int = 3) -> list[str]:
        """Fallback localization when normal filters produce zero candidates."""
        pool = list(self._localize_fallback_pool)
        if not pool:
            self._log_service("[Localize Fallback] No localization findings available.")
            return []

        component_summary: list[dict] = []
        grouped: dict[str, list[dict]] = {}
        for item in pool:
            comp = str(item.get("component") or "").strip()
            if comp:
                grouped.setdefault(comp, []).append(item)

        for comp, items in grouped.items():
            severities = []
            kpis = []
            reasons = []
            times = []
            problematic_true = 0
            filtered_count = 0
            for item in items:
                try:
                    severities.append(float(item.get("severity", 0.0) or 0.0))
                except (TypeError, ValueError):
                    pass
                kpi_name = str(item.get("kpi") or "").strip()
                if kpi_name and kpi_name not in kpis:
                    kpis.append(kpi_name)
                reason_hint = str(item.get("reason_hint") or "").strip()
                if reason_hint and reason_hint not in reasons:
                    reasons.append(reason_hint)
                t = str(item.get("time") or "").strip()
                if t:
                    times.append(t)
                if item.get("value_is_problematic") is True:
                    problematic_true += 1
                if bool(item.get("filtered")):
                    filtered_count += 1
            component_summary.append(
                {
                    "component": comp,
                    "count": len(items),
                    "max_severity": max(severities) if severities else 0.0,
                    "avg_severity": (sum(severities) / len(severities)) if severities else 0.0,
                    "kpis": kpis,
                    "reason_hints": reasons[:6],
                    "times": sorted(set(times))[:6],
                    "problematic_true_count": problematic_true,
                    "filtered_count": filtered_count,
                }
            )

        component_summary.sort(
            key=lambda x: (-int(x["count"]), -float(x["max_severity"]), -float(x["avg_severity"]))
        )
        prompt = (
            _LOCALIZE_FALLBACK_PROMPT
            + "\n\nComponent summary:\n"
            + json.dumps(component_summary, ensure_ascii=False, indent=2)
            + "\n\nRaw findings:\n"
            + json.dumps(pool, ensure_ascii=False, indent=2)
        )
        try:
            raw = get_chat_completion(
                [{"role": "user", "content": prompt}],
                self.configs,
                temperature=0.0,
            )
            self._log_service(f"[Localize Fallback] LLM response:\n{raw}")
            payload = json.loads(self._extract_json(raw))
        except Exception as e:
            self._log_service(f"[Localize Fallback] LLM failed: {e}")
            return []

        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            self._log_service("[Localize Fallback] Invalid response: missing candidates list.")
            return []

        created_ids: list[str] = []
        seen_components: set[str] = set()
        for entry in candidates:
            if len(created_ids) >= top_k or not isinstance(entry, dict):
                break
            comp = str(entry.get("component") or "").strip()
            if not comp or comp in seen_components:
                continue
            seen_components.add(comp)
            items = grouped.get(comp, [])
            if not items:
                continue
            normalized_time = self._normalize_outlier_time(
                str(entry.get("time") or "").strip()
            ) or str(entry.get("time") or "").strip()
            if not normalized_time:
                normalized_time = str(items[0].get("time") or "").strip()
            reason_hint = str(entry.get("reason_hint") or "").strip()
            if not reason_hint:
                reason_hint = str(items[0].get("reason_hint") or "").strip()
            selected_kpis = entry.get("problematic_kpis")
            if isinstance(selected_kpis, str):
                selected_kpis = [
                    x.strip() for x in re.split(r"[,\n]", selected_kpis) if x.strip()
                ]
            elif isinstance(selected_kpis, (list, tuple)):
                selected_kpis = [str(x).strip() for x in selected_kpis if str(x).strip()]
            else:
                selected_kpis = []
            try:
                confidence = float(entry.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            why = str(entry.get("why") or "").strip()
            max_severity = max(
                float(item.get("severity", 0.0) or 0.0) for item in items
            )
            kpi_list = []
            for item in items:
                kpi_name = str(item.get("kpi") or "").strip()
                if kpi_name and kpi_name not in kpi_list:
                    kpi_list.append(kpi_name)
            if selected_kpis:
                merged_kpis: list[str] = []
                for k in selected_kpis + kpi_list:
                    if k and k not in merged_kpis:
                        merged_kpis.append(k)
                kpi_list = merged_kpis
            evidence_parts = [
                "fallback_localize",
                f"count={len(items)}",
                f"max_severity={max_severity:.0f}",
            ]
            if kpi_list:
                evidence_parts.append("kpis=" + ", ".join(kpi_list[:6]))
            if why:
                evidence_parts.append(why)
            nid = self.tree.add_candidate(
                stage="localize",
                component=comp,
                reason=reason_hint or None,
                time_str=normalized_time or None,
                kpi=kpi_list,
                evidence=" | ".join(evidence_parts),
                severity=max_severity,
                confidence=confidence,
            )
            created_ids.append(nid)
            self._log_service(
                f"[Localize Fallback] + {comp} "
                f"(time={normalized_time!r}, reason={reason_hint!r}, confidence={confidence:.2f}, "
                f"count={len(items)}, max_severity={max_severity:.0f})"
            )
            self._update_live_view()

        if not created_ids:
            self._log_service("[Localize Fallback] LLM returned no usable candidates.")
        return created_ids

    def _scan_kpi(self, comp_type: str, kpi: str) -> list[dict]:
        """Call get_kpi_peer_graph + vision critic, then apply numeric self-baseline gate."""
        try:
            result = self.actions.get_kpi_peer_graph(
                self.namespace, comp_type, kpi,
            )
        except Exception as e:
            self._log_service(f"[{comp_type}/{kpi}] get_kpi_peer_graph failed: {e}")
            return []

        image_paths = self._extract_image_paths(result)
        if not image_paths:
            return []

        kpi_hint = self._get_kpi_problem_hint(comp_type, kpi)
        analysis = self._call_vision_critic(
            image_paths, context=f"{comp_type}/{kpi} | {kpi_hint}",
        )
        parsed, filtered_aux = self._parse_outliers(
            analysis,
            kpi,
            include_filtered_aux=True,
        )
        self._store_filtered_localize_aux(filtered_aux)
        self._record_localize_fallback_items(
            parsed,
            source=f"{comp_type}/{kpi}",
            filtered=False,
        )
        self._record_localize_fallback_items(
            filtered_aux,
            source=f"{comp_type}/{kpi}",
            filtered=True,
        )
        return parsed
        # return self._filter_outliers_by_self_baseline(parsed, kpi, comp_type)

    def _scan_traces(self) -> list[dict]:
        """Scan trace peer volume/latency/error across relevant component families."""
        outliers: list[dict] = []

        trace_targets = self._get_trace_scan_targets()
        for component_type, role in trace_targets:
            try:
                vol_result = self.actions.get_trace_volume_peer_graph(
                    self.namespace, component_type, role=role,
                )
                vol_images = self._extract_image_paths(vol_result)
                if vol_images:
                    analysis = self._call_vision_critic(
                        vol_images, context=f"trace_peer_volume/{component_type}/{role}",
                    )
                    self._log_service(
                        f"[Trace peer volume {component_type}/{role}] Vision critic:\n{analysis}"
                    )
            except Exception as e:
                self._log_service(
                    f"[Trace peer volume {component_type}/{role}] Failed: {e}"
                )

            try:
                latency_result = self.actions.get_trace_latency_peer_graph(
                    self.namespace, component_type, role=role,
                )
                latency_images = self._extract_image_paths(latency_result)
                if latency_images:
                    analysis = self._call_vision_critic(
                        latency_images, context=f"trace_peer_latency/{component_type}/{role}",
                    )
                    parsed = self._parse_outliers(analysis, "trace_latency")
                    outliers.extend(parsed)
            except Exception as e:
                self._log_service(
                    f"[Trace peer latency {component_type}/{role}] Failed: {e}"
                )

            try:
                error_result = self.actions.get_trace_error_peer_graph(
                    self.namespace, component_type, role=role,
                )
                error_images = self._extract_image_paths(error_result)
                if error_images:
                    analysis = self._call_vision_critic(
                        error_images, context=f"trace_peer_error_rate/{component_type}/{role}",
                    )
                    parsed = self._parse_outliers(analysis, "trace_error_rate")
                    outliers.extend(parsed)
            except Exception as e:
                self._log_service(
                    f"[Trace peer error {component_type}/{role}] Failed: {e}"
                )

        return outliers

    def _get_trace_scan_targets(self) -> list[tuple[str, str]]:
        """Use combined peer views for both caller and callee trace scans."""
        return [("all", "caller"), ("all", "callee")]

    def _precompute_trace_edges(self, candidate_ids: list[str]):
        """Build raw trace edges and precompute anomalous trace caller→callee edges."""
        # Collect unique components from localization stage
        components = {
            self.tree.nodes[cid].component
            for cid in candidate_ids
            if cid in self.tree.nodes
        }
        if not components:
            return
        # Load trace window once and build adjacency
        if not hasattr(self.actions, "_load_trace_window"):
            return
        try:
            df = self.actions._load_trace_window(self.namespace)
        except Exception:
            return
        if df.empty or "cmdb_id" not in df.columns or "dsName" not in df.columns:
            return
        self._trace_anomaly_edges = self._build_trace_anomaly_edge_cache(df)
        # caller → callee edges where either side is in localized components
        sub = df[["cmdb_id", "dsName"]].dropna()
        seen_edges: set[tuple[str, str]] = set()
        for _, row in sub.iterrows():
            caller = str(row["cmdb_id"])
            callee = str(row["dsName"])
            if caller in components or callee in components:
                edge = (caller, callee)
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                self.tree.add_trace_edge(caller, callee)

    def _build_trace_anomaly_edge_cache(self, df) -> list[dict]:
        """Precompute anomalous caller->callee edge events using dataset renderer."""
        renderer = get_trace_path_renderer(getattr(self.problem, "problem_id", self.profile.name or ""))
        if not hasattr(renderer, "build_edge_events"):
            return []
        try:
            dataset_name = str((self.profile.name or "")).replace("openrca_", "").lower()
            trace_min_edge_volume = 5 if dataset_name.startswith("market") else 20
            _edge_metric_df, _anomalies, events = renderer.build_edge_events(
                self.actions,
                self.namespace,
                window_minutes=30,
                min_edge_volume=trace_min_edge_volume,
                sustain_buckets=2,
            )
        except Exception as e:
            self._log_service(f"[Trace] Failed to precompute anomalous edges: {e}")
            return []
        out = list(events or [])
        if out:
            self._log_service(
                f"[Trace] Precomputed {len(out)} anomalous caller-callee edge events via dataset renderer."
            )
        return out

    def _save_trace_anomaly_edges(self) -> None:
        """Persist precomputed anomalous trace edges beside other task artifacts."""
        output_path = self.save_dir / "trace_anomaly_edges.json"
        payload = {
            "query_window": self.time_range,
            "edge_count": len(self._trace_anomaly_edges),
            "edges": self._trace_anomaly_edges,
        }
        try:
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            )
            self._log_service(
                f"Saved precomputed trace anomaly edges to {output_path}"
            )
        except Exception as e:
            self._log_service(
                f"[Trace] Failed to save precomputed anomaly edges: {e}"
            )

    def _format_trace_relation_badge(self, anomaly_type: str, time_str: str | None) -> str:
        """Compact badge text for directional trace expand relations."""
        raw = str(anomaly_type or "").strip().lower()
        metric = {
            "latency_edge": "latency-edge",
            "error_rate": "error-edge",
            "network_gap": "gap-edge",
            "remote_process_time": "remote-node",
        }.get(raw, (raw or "trace").replace("_", "-"))
        if time_str:
            try:
                clock = str(time_str).split()[1] if " " in str(time_str) else str(time_str)
            except Exception:
                clock = str(time_str)
            return f"{metric}@{clock}"
        return metric

    @staticmethod
    def _is_time_within_minutes(center_time: str | None, event_time: str | None, window_min: int = 5) -> bool:
        """Return True when event_time is within ±window_min from center_time."""
        c = str(center_time or "").strip()
        e = str(event_time or "").strip()
        if not c or not e:
            return False
        try:
            tc = datetime.strptime(c, "%Y-%m-%d %H:%M:%S")
            te = datetime.strptime(e, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        return abs((te - tc).total_seconds()) <= float(window_min) * 60.0

    def _get_trace_relation_candidates(
        self,
        component: str,
        *,
        anchor_time: str | None = None,
        window_min: int | None = None,
    ) -> tuple[list[str], dict[str, list[dict]]]:
        """Return expand candidates connected by precomputed anomalous trace edges."""
        aliases = set(self._component_dependency_aliases(component))
        if not aliases or not self._trace_anomaly_edges:
            return [], {}

        rel_map: dict[str, list[dict]] = {}
        for edge in self._trace_anomaly_edges:
            caller = str(edge.get("caller") or "").strip()
            callee = str(edge.get("callee") or "").strip()
            if not caller or not callee:
                continue
            edge_time = str(edge.get("time") or "").strip()
            if anchor_time and window_min is not None:
                if not self._is_time_within_minutes(anchor_time, edge_time, window_min):
                    continue
            if caller in aliases:
                target = callee
                rel_type = "call_downstream"
            elif callee in aliases:
                target = caller
                rel_type = "call_upstream"
            else:
                continue
            if target == component:
                continue
            rel_map.setdefault(target, []).append(
                {
                    "relation_family": "trace_call",
                    "relation_type": rel_type,
                    "anomaly_type": str(edge.get("anomaly_type") or "mixed"),
                    "time": edge_time,
                    "end_time": str(edge.get("end_time") or "").strip(),
                    "duration_minutes": edge.get("duration_minutes"),
                    "label": self._format_trace_relation_badge(
                        str(edge.get("anomaly_type") or "mixed"),
                        edge_time,
                    ),
                    "metadata": {
                        "edge_id": edge.get("edge_id"),
                        "caller": caller,
                        "callee": callee,
                        **(edge.get("metadata") or {}),
                    },
                }
            )

        for comp, items in list(rel_map.items()):
            deduped: list[dict] = []
            seen: set[tuple[str, str, str]] = set()
            for item in sorted(
                items,
                key=lambda x: (
                    x.get("time") or "",
                    x.get("relation_type") or "",
                    x.get("anomaly_type") or "",
                ),
            ):
                key = (
                    str(item.get("relation_type") or ""),
                    str(item.get("anomaly_type") or ""),
                    str(item.get("time") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            rel_map[comp] = deduped
        return sorted(rel_map.keys()), rel_map

    def _get_deployment_relation_candidates(
        self,
        component: str,
    ) -> tuple[list[str], dict[str, list[dict]], list[dict]]:
        """Return deployment sibling candidates plus containment-group metadata."""
        graphs = get_graphs(self._get_dependency_graph_dataset_key())
        deployment_graph = graphs.get("deployment_graph") or {}
        aliases = self._component_dependency_aliases(component)
        if not aliases:
            return [], {}, []

        rel_map: dict[str, list[dict]] = {}
        groups: list[dict] = []
        seen_groups: set[str] = set()
        for alias in aliases:
            if alias in deployment_graph:
                members = list(deployment_graph.get(alias) or [])
                for member in members:
                    rel_map.setdefault(member, []).append(
                        {
                            "relation_family": "deployment_contains",
                            "relation_type": "deploy_child",
                            "anomaly_type": None,
                            "time": None,
                            "label": f"group={alias}",
                            "metadata": {
                                "container_component": alias,
                                "member_components": members,
                            },
                        }
                    )
                if alias not in seen_groups:
                    seen_groups.add(alias)
                    groups.append(
                        {
                            "container_component": alias,
                            "member_components": members,
                        }
                    )
            for host, members in deployment_graph.items():
                if alias not in members:
                    continue
                siblings = [member for member in members if member != alias]
                for sibling in siblings:
                    rel_map.setdefault(sibling, []).append(
                        {
                            "relation_family": "deployment_contains",
                            "relation_type": "deploy_sibling",
                            "anomaly_type": None,
                            "time": None,
                            "label": f"group={host}",
                            "metadata": {
                                "container_component": host,
                                "member_components": list(members),
                            },
                        }
                    )
                if host not in seen_groups:
                    seen_groups.add(host)
                    groups.append(
                        {
                            "container_component": host,
                            "member_components": list(members),
                        }
                    )

        for comp, items in list(rel_map.items()):
            deduped: list[dict] = []
            seen: set[tuple[str, str]] = set()
            for item in items:
                key = (
                    str(item.get("relation_type") or ""),
                    str((item.get("metadata") or {}).get("container_component") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            rel_map[comp] = deduped
        return sorted(rel_map.keys()), rel_map, groups

    @staticmethod
    def _merge_relation_candidate_maps(*relation_maps: dict[str, list[dict]]) -> dict[str, list[dict]]:
        """Merge per-component relation metadata maps."""
        merged: dict[str, list[dict]] = {}
        for rel_map in relation_maps:
            for component, items in (rel_map or {}).items():
                merged.setdefault(component, []).extend(items or [])
        return merged

    def _get_topology_relation_candidates(
        self,
        component: str,
    ) -> tuple[list[str], dict[str, list[dict]]]:
        """Return static topology candidates from graph.py for controller expand.

        Unlike `_get_trace_relation_candidates`, these relations do not depend on
        precomputed anomalous trace edges. They come from the dataset's static
        call/shared-resource/deployment graphs and act as a fallback/augment when
        trace-based dependency extraction is sparse.
        """
        dataset_key = self._get_dependency_graph_dataset_key()
        related_components, relation_types = get_related_components_with_relations(
            component,
            dataset_key,
        )
        if not related_components:
            return [], {}

        rel_map: dict[str, list[dict]] = {}
        for target in related_components:
            target = str(target or "").strip()
            if not target or target == component:
                continue
            rels = relation_types.get(target) or []
            for rel in rels:
                rel_name = str(rel or "").strip()
                if not rel_name:
                    continue
                family = "topology_graph"
                if rel_name.startswith("call_"):
                    family = "graph_call"
                elif rel_name.startswith("deploy_"):
                    family = "graph_deploy"
                elif rel_name.startswith("shared_"):
                    family = "graph_shared"
                rel_map.setdefault(target, []).append(
                    {
                        "relation_family": family,
                        "relation_type": rel_name,
                        "anomaly_type": None,
                        "time": None,
                        "label": "topology",
                        "metadata": {
                            "dataset_key": dataset_key,
                            "source_component": component,
                        },
                    }
                )

        for comp, items in list(rel_map.items()):
            deduped: list[dict] = []
            seen: set[str] = set()
            for item in items:
                key = str(item.get("relation_type") or "")
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)
            rel_map[comp] = deduped

        return sorted(rel_map.keys()), rel_map

    def _relation_label_from_infos(self, rel_infos: list[dict]) -> str | None:
        """Compact node-level relation label for expand nodes."""
        if not rel_infos:
            return None
        parts: list[str] = []
        seen: set[str] = set()
        for info in rel_infos:
            relation_type = str(info.get("relation_type") or "").strip()
            label = str(info.get("label") or "").strip()
            piece = relation_type if not label else f"{relation_type}:{label}"
            if piece and piece not in seen:
                seen.add(piece)
                parts.append(piece)
        return ", ".join(parts[:3]) if parts else None

    def _attach_expand_relation_edges(
        self,
        current_node_id: str,
        current_component: str,
        child_node_id: str,
        child_component: str,
        rel_infos: list[dict],
    ) -> None:
        """Persist directional non-tree relation edges for one expand candidate."""
        for info in rel_infos:
            relation_family = str(info.get("relation_family") or "").strip() or None
            relation_type = str(info.get("relation_type") or "").strip() or None
            anomaly_type = info.get("anomaly_type")
            time_str = str(info.get("time") or "").strip() or None
            label = str(info.get("label") or "").strip() or None
            metadata = dict(info.get("metadata") or {})
            if relation_type == "call_upstream":
                src_node_id, dst_node_id = child_node_id, current_node_id
                src_component, dst_component = child_component, current_component
            else:
                src_node_id, dst_node_id = current_node_id, child_node_id
                src_component, dst_component = current_component, child_component
            self.tree.add_relation_edge(
                src_node_id,
                dst_node_id,
                src_component=src_component,
                dst_component=dst_component,
                relation_family=relation_family,
                relation_type=relation_type,
                anomaly_type=anomaly_type,
                time=time_str,
                label=label,
                metadata=metadata,
            )

    def _register_deployment_containment_for_expand(
        self,
        current_node_id: str,
        current_component: str,
        new_candidate_ids: list[str],
        groups: list[dict],
    ) -> None:
        """Create/update visual host-containment groups for deployment-based expand."""
        if not groups:
            return
        node_ids_by_component: dict[str, list[str]] = {}
        for nid, node in self.tree.nodes.items():
            comp = (node.component or "").strip()
            if comp:
                node_ids_by_component.setdefault(comp, []).append(nid)

        for group in groups:
            host = str(group.get("container_component") or "").strip()
            member_components = [
                str(comp or "").strip()
                for comp in (group.get("member_components") or [])
                if str(comp or "").strip()
            ]
            if not host or not member_components:
                continue
            member_ids: list[str] = []
            if (
                (current_component == host or current_component in member_components)
                and current_node_id in self.tree.nodes
            ):
                member_ids.append(current_node_id)
            for comp in member_components:
                for nid in node_ids_by_component.get(comp, []):
                    if nid in new_candidate_ids and nid not in member_ids:
                        member_ids.append(nid)
            if len(member_ids) < 2:
                continue
            self.tree.upsert_containment_group(
                None,
                member_ids,
                container_component=host,
                relation_family="deployment_contains",
                relation_type="contains",
                label=host,
                metadata={"member_components": member_components},
            )

    def _build_localization_host_containment_groups(
        self, candidate_ids: list[str],
    ) -> None:
        """Group localization candidates by OS host; when 2+ are on same host, create containment group."""
        try:
            graphs = get_graphs(self._get_dependency_graph_dataset_key())
            deploy_g = graphs.get("deployment_graph") or {}
            if not deploy_g:
                return
            # host -> [components on that host]
            host_to_components: dict[str, list[str]] = dict(deploy_g)
            component_to_host: dict[str, str] = {}
            for host, comps in host_to_components.items():
                for c in comps:
                    component_to_host[c] = host
            # Group candidate node IDs by their host
            host_to_node_ids: dict[str, list[str]] = {}
            for cid in candidate_ids:
                node = self.tree.nodes.get(cid)
                if not node:
                    continue
                comp = (node.component or "").strip()
                if not comp:
                    continue
                host = component_to_host.get(comp)
                if host:
                    host_to_node_ids.setdefault(host, []).append(cid)
            for host, member_ids in host_to_node_ids.items():
                if len(member_ids) >= 2:
                    member_components = []
                    for nid in member_ids:
                        n = self.tree.nodes.get(nid)
                        if n and (n.component or "").strip():
                            member_components.append(n.component.strip())
                    self.tree.upsert_containment_group(
                        None,
                        member_ids,
                        container_component=host,
                        relation_family="deployment_contains",
                        relation_type="contains",
                        label=f"{host} ({', '.join(member_components)})",
                        metadata={"member_components": member_components},
                    )
                    self._log_service(
                        f"[Localize] Grouped {len(member_ids)} candidates on {host}: "
                        f"{', '.join(member_components)}"
                    )
        except Exception as e:
            self._log_service(
                f"[Localize] Failed to build host containment groups: {e}"
            )

    def _sort_candidates_by_priority(self, candidate_ids: list[str]) -> list[str]:
        """Sort candidate IDs by priority: highest severity first, then highest confidence."""
        def key(cid: str):
            n = self.tree.nodes.get(cid)
            if not n:
                return (0.0, 0.0)
            sev = getattr(n, "severity", 0.0) or 0.0
            conf = n.confidence or 0.0
            return (-sev, -conf)
        return sorted(candidate_ids, key=key)

    @staticmethod
    def _parse_related_components_from_text(text: str) -> list[str]:
        """Extract component names from 'related_components: A, B' or '- A' lines."""
        comps = []
        for line in text.splitlines():
            line = line.strip()
            if "related_components" in line.lower() or "related components" in line.lower():
                after = re.split(r"related_components?\s*[=:]\s*", line, maxsplit=1, flags=re.I)
                if len(after) > 1:
                    rest = after[1].strip().strip("[]")
                    comps.extend(re.split(r"[,;]", rest))
            if line.startswith("- ") and len(line) > 2:
                comps.append(line[2:].strip().split()[0])
        return [c.strip() for c in comps if c.strip() and len(c.strip()) > 1]

    def _render_localization_timeline(self, tree_path: Path):
        """Render Manim scene with localization candidates on x-axis = time."""
        script_path = Path(__file__).parent / "visualize_rca_tree.py"
        out_dir = self.save_dir / "media"
        cmd = [
            "manim", "-ql", str(script_path), "LocalizationTimelineScene",
            "--media_dir", str(out_dir),
        ]
        env = {**os.environ, "TREE_JSON": str(tree_path)}
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
            if r.returncode != 0 and r.stderr:
                self._log_service(f"Manim timeline render: {r.stderr[:400]}")
        except FileNotFoundError:
            self._log_service("manim not found; skip localization timeline")
        except subprocess.TimeoutExpired:
            self._log_service("Manim timeline render timed out")

    def _parse_localization_datetime(self, time_str: str | None) -> datetime | None:
        """Parse localization time, accepting both full timestamps and time-only strings."""
        if not time_str:
            return None

        s = str(time_str).strip()
        s = re.sub(r"\s*(UTC|Z)$", "", s, flags=re.I)
        for prefix in ("approx ", "approximately ", "~", "around ", "at "):
            if s.lower().startswith(prefix):
                s = s[len(prefix):].strip()

        full_formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%dT%H:%M:%S",
        ]
        for fmt in full_formats:
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

        time_only_match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
        if not time_only_match:
            return None

        try:
            hour = int(time_only_match.group(1))
            minute = int(time_only_match.group(2))
            second = int(time_only_match.group(3) or 0)
        except ValueError:
            return None

        base_dt = None
        if isinstance(self.time_range, dict):
            start_raw = self.time_range.get("start")
            if isinstance(start_raw, datetime):
                base_dt = start_raw.astimezone(timezone.utc)
            elif isinstance(start_raw, (int, float)):
                base_dt = datetime.fromtimestamp(float(start_raw), tz=timezone.utc)

        if base_dt is None:
            base_dt = datetime.now(timezone.utc)

        return base_dt.replace(
            hour=hour,
            minute=minute,
            second=second,
            microsecond=0,
        )

    def _build_localization_dependency_item(self, candidate_id: str) -> dict | None:
        """Build normalized metadata for a localization candidate."""
        node = self.tree.nodes.get(candidate_id)
        if not node or node.stage != "localize":
            return None
        return {
            "id": candidate_id,
            "component": node.component or "?",
            "time": node.time or "",
            "kpi": node.kpi or "",
            "reason": node.reason or "",
            "severity": float(getattr(node, "severity", 0.0) or 0.0),
            "dt": self._parse_localization_datetime(node.time),
        }

    def _finalize_localization_cluster(
        self,
        items: list[dict],
        min_dt: datetime | None,
        max_dt: datetime | None,
    ) -> dict:
        """Sort cluster items and return normalized cluster payload."""
        return {
            "items": sorted(
                items,
                key=lambda it: (-it["severity"], it["component"], it["time"]),
            ),
            "min_dt": min_dt,
            "max_dt": max_dt,
        }

    def _cluster_localization_candidates(
        self,
        candidate_ids: list[str],
        window_minutes: int = _LOCALIZATION_DEP_CLUSTER_WINDOW_MINUTES,
    ) -> list[dict]:
        """Group localization candidates so each cluster spans at most N minutes."""
        items = [
            item
            for cid in candidate_ids
            for item in [self._build_localization_dependency_item(cid)]
            if item is not None
        ]

        if not items:
            return []

        parseable = sorted(
            (it for it in items if it["dt"] is not None),
            key=lambda it: (it["dt"], -it["severity"], it["component"]),
        )
        unknown_time = [it for it in items if it["dt"] is None]

        clusters: list[dict] = []
        current: list[dict] = []
        current_min: datetime | None = None
        current_max: datetime | None = None
        max_span = timedelta(minutes=window_minutes)

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
                clusters.append(
                    self._finalize_localization_cluster(current, current_min, current_max)
                )
                current = [item]
                current_min = item_dt
                current_max = item_dt

        if current:
            clusters.append(
                self._finalize_localization_cluster(current, current_min, current_max)
            )

        for item in unknown_time:
            clusters.append(self._finalize_localization_cluster([item], None, None))

        return clusters

    def _component_dependency_aliases(self, component: str | None) -> list[str]:
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

    def _get_localization_dependency_relation(
        self,
        source_component: str,
        target_component: str,
        graphs: dict[str, dict],
    ) -> str | None:
        """Return the directed dependency type from source -> target, if any."""
        source_aliases = self._component_dependency_aliases(source_component)
        target_aliases = set(self._component_dependency_aliases(target_component))
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

    def _get_dependency_graph_dataset_key(self) -> str:
        """Return the dataset key expected by `get_graphs()`."""
        return self.profile.name or ""

    def _build_localization_dependency_edges(self, clusters: list[dict]) -> list[dict]:
        """Connect localized anomalies when a directed dependency exists."""
        if not clusters:
            return []

        graphs = get_graphs(self._get_dependency_graph_dataset_key())
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
                relation = self._get_localization_dependency_relation(
                    source["component"],
                    target["component"],
                    graphs,
                )
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
                _LOCALIZATION_DEP_RELATION_ORDER.get(edge["relation"], 99),
                edge["source_id"],
                edge["target_id"],
            ),
        )

    def _format_localization_dependency_cluster_title(self, cluster_idx: int, cluster: dict) -> str:
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

    def _get_localization_dependency_node_face_color(self, severity: float) -> str:
        """Map anomaly severity to a node fill color."""
        if severity >= 80:
            return "#FDECEC"
        if severity >= 50:
            return "#FFF4E5"
        return "#ECF5FF"

    def _format_localization_dependency_node_label(self, item: dict) -> str:
        """Build the node label for the dependency graph."""
        metric_text = item["kpi"] or item["reason"] or "anomaly"
        if len(metric_text) > 28:
            metric_text = metric_text[:25] + "..."

        time_text = item["time"]
        if len(time_text) >= 19:
            time_text = time_text[11:16]

        return f"{item['component']}\n{time_text} | {metric_text}"

    def _draw_localization_dependency_cluster(
        self,
        ax,
        cluster_idx: int,
        cluster: dict,
        positions: dict[str, tuple[float, float]],
        box_width: float,
        box_height: float,
        FancyBboxPatch,
    ) -> None:
        """Draw one clustered dependency group and its nodes."""
        items = cluster["items"]
        x_center = cluster_idx * _LOCALIZATION_DEP_CLUSTER_GAP
        local_positions: list[float] = []

        for item_idx, item in enumerate(items):
            y = (len(items) - 1) / 2.0 - item_idx
            local_positions.append(y)
            positions[item["id"]] = (x_center, y)

        y_min = min(local_positions) - 0.75
        y_max = max(local_positions) + 0.75
        cluster_patch = FancyBboxPatch(
            (x_center - 1.7, y_min),
            3.4,
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
            x_center,
            y_max + 0.2,
            self._format_localization_dependency_cluster_title(cluster_idx, cluster),
            ha="center",
            va="bottom",
            fontsize=9,
            fontweight="bold",
            color="#333333",
        )

        for item in items:
            x, y = positions[item["id"]]
            node_patch = FancyBboxPatch(
                (x - box_width / 2.0, y - box_height / 2.0),
                box_width,
                box_height,
                boxstyle="round,pad=0.03,rounding_size=0.08",
                facecolor=self._get_localization_dependency_node_face_color(item["severity"]),
                edgecolor="#4F5B67",
                linewidth=1.2,
                zorder=2,
            )
            ax.add_patch(node_patch)
            ax.text(
                x,
                y,
                self._format_localization_dependency_node_label(item),
                ha="center",
                va="center",
                fontsize=8,
                color="#1F2933",
                zorder=3,
            )

    def _draw_localization_dependency_edge(
        self,
        ax,
        edge_idx: int,
        edge: dict,
        positions: dict[str, tuple[float, float]],
        box_width: float,
    ) -> None:
        """Draw one directed dependency edge and its relation label."""
        source_pos = positions.get(edge["source_id"])
        target_pos = positions.get(edge["target_id"])
        if not source_pos or not target_pos:
            return

        sx, sy = source_pos
        tx, ty = target_pos
        relation = edge["relation"]
        color = _LOCALIZATION_DEP_RELATION_COLORS.get(relation, "#7F8C8D")
        same_cluster = edge["source_cluster"] == edge["target_cluster"]
        rad = 0.20 if same_cluster else 0.0
        if same_cluster and edge_idx % 2:
            rad *= -1

        ax.annotate(
            "",
            xy=(tx - box_width / 2.0 + 0.08, ty),
            xytext=(sx + box_width / 2.0 - 0.08, sy),
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
        mid_y = (sy + ty) / 2.0 + (0.18 if same_cluster else 0.10)
        ax.text(
            mid_x,
            mid_y,
            relation,
            fontsize=7,
            color=color,
            ha="center",
            va="center",
            bbox={"boxstyle": "round,pad=0.18", "fc": "white", "ec": "none", "alpha": 0.85},
            zorder=4,
        )

    def _render_localization_dependency_graph(self, candidate_ids: list[str]) -> None:
        """Render localization anomalies as 10-minute clusters linked by dependencies."""
        clusters = self._cluster_localization_candidates(
            candidate_ids,
            window_minutes=_LOCALIZATION_DEP_CLUSTER_WINDOW_MINUTES,
        )
        if not clusters:
            return

        try:
            import matplotlib.pyplot as plt
            from matplotlib.lines import Line2D
            from matplotlib.patches import FancyBboxPatch
        except Exception:
            self._log_service(
                "matplotlib not available; skip localization dependency graph"
            )
            return

        output_path = self.save_dir / "localization_dependency_graph.png"
        edges = self._build_localization_dependency_edges(clusters)

        max_items = max(len(cluster["items"]) for cluster in clusters)
        fig_width = max(12.0, 4.6 * len(clusters))
        fig_height = max(6.0, 1.5 * max_items + 2.5)
        fig, ax = plt.subplots(figsize=(fig_width, fig_height))
        positions: dict[str, tuple[float, float]] = {}

        for cluster_idx, cluster in enumerate(clusters):
            self._draw_localization_dependency_cluster(
                ax,
                cluster_idx,
                cluster,
                positions,
                _LOCALIZATION_DEP_BOX_WIDTH,
                _LOCALIZATION_DEP_BOX_HEIGHT,
                FancyBboxPatch,
            )

        for edge_idx, edge in enumerate(edges):
            self._draw_localization_dependency_edge(
                ax,
                edge_idx,
                edge,
                positions,
                _LOCALIZATION_DEP_BOX_WIDTH,
            )

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
                ("call dependency", _LOCALIZATION_DEP_RELATION_COLORS["call"]),
                ("shared resource dependency", _LOCALIZATION_DEP_RELATION_COLORS["shared"]),
                ("deployment dependency", _LOCALIZATION_DEP_RELATION_COLORS["deploy"]),
            )
        ]
        ax.legend(handles=legend_handles, loc="upper right", frameon=False, fontsize=8)
        ax.set_title(
            "Localization Dependency Graph (10-minute clusters)",
            fontsize=12,
            pad=16,
        )
        ax.set_axis_off()
        ax.set_xlim(-2.3, (len(clusters) - 1) * _LOCALIZATION_DEP_CLUSTER_GAP + 2.3)
        ax.set_ylim(-max_items / 2.0 - 1.6, max_items / 2.0 + 2.0)

        fig.tight_layout()
        try:
            fig.savefig(output_path, dpi=160, bbox_inches="tight")
        finally:
            plt.close(fig)
        self._log_service(
            f"Saved localization dependency graph to {output_path}"
        )

    def _format_deployment_graph_text(self) -> str:
        """Return deployment graph as text (host → components) for prompt injection."""
        graphs = get_graphs(self._get_dependency_graph_dataset_key())
        deploy_g = graphs.get("deployment_graph") or {}
        if not deploy_g:
            return ""
        lines = []
        for host in sorted(deploy_g.keys()):
            comps = deploy_g.get(host) or []
            if comps:
                lines.append(f"{host} → {', '.join(sorted(comps))}")
        return "\n".join(lines) if lines else ""

    def _map_component_by_trace_kpi(self, component: str, anomalous_kpi: str) -> str:
        """Normalize component level by trace KPI semantics.

        - network_gap -> prefer OS node (host)
        - remote_process_time -> prefer docker
        """
        comp = str(component or "").strip()
        kpi = str(anomalous_kpi or "").strip().lower()
        if not comp:
            return comp
        level = self._get_level(comp)
        graphs = get_graphs(self._get_dependency_graph_dataset_key())
        deploy_g = graphs.get("deployment_graph") or {}
        if kpi == "network_gap":
            if level == "node":
                return comp
            if level == "docker":
                for host, members in deploy_g.items():
                    if comp in (members or []):
                        return str(host).strip() or comp
        elif kpi == "remote_process_time":
            if level == "docker":
                return comp
            if level == "node":
                members = list(deploy_g.get(comp) or [])
                for m in members:
                    if self._get_level(str(m)) == "docker":
                        return str(m).strip()
        return comp

    def _build_controller_system_prompt(self, stage: str, extra: str = "") -> str:
        """Build system prompt with available actions for controller stage."""
        actions_desc = {}
        if self.problem:
            actions_desc = self.problem.get_available_actions()
        lines = [
            "You are an RCA analyst. Output exactly ONE action per message in a code block.",
            "Available actions (call with correct arguments):",
        ]
        for name, doc in (actions_desc or {}).items():
            lines.append(f"  - {name}: {doc[:200]}")
        lines.append("")
        lines.append(
            "When you have enough evidence, output your conclusion with "
            "VERDICT: and/or confidence = <float 0-1> and a brief explanation."
        )
        if extra:
            lines.append(extra)
        return "\n".join(lines)

    # ── Stage 2: Deep Dive (controller-driven) ───────────────────────

    def _stage2_controller_deep_dive(self, candidate_ids: list[str]) -> list[str]:
        """Deep dive: use actions to find which root cause class (reason) best matches
        this component in the -5/+5 min window; may prune if not a real problem.
        """
        if not candidate_ids or not self.problem:
            return []
        cid = candidate_ids[0]
        node = self.tree.nodes[cid]
        level = self._get_level(node.component)
        # Hint for the agent about component level (node / pod / service) and
        # which KPIs are typically relevant for this level.
        kpi_hint = ""
        try:
            # 1) Short core list (for quick glance) from kpis_by_type, when available.
            core_kpis = self.profile.kpis_by_type.get(level) or []
            # 2) Full non-dead KPI catalog from metric files (by metric type),
            #    wired via kpi_catalog_*.json.
            full_by_type = getattr(self.profile, "full_kpis_by_type", {}) or {}
            dataset = (self.profile.name or "").replace("openrca_", "")
            metric_types: list[str] = []
            if dataset.startswith("telecom"):
                # Telecom: os -> node metrics, docker -> container, db/redis -> service/middleware
                if level == "node":
                    metric_types = ["node"]
                elif level == "pod":
                    metric_types = ["container"]
                elif level == "service":
                    metric_types = ["service", "middleware"]
            elif dataset.startswith("market"):
                # Market: node ↔ metric_node, pod ↔ metric_container, service ↔ metric_service
                if level == "node":
                    metric_types = ["node"]
                elif level == "pod":
                    metric_types = ["container"]
                elif level == "service":
                    metric_types = ["service"]
            elif dataset.startswith("bank"):
                # Bank: node-level reasons; use both app + container KPIs.
                metric_types = ["app", "container"]

            full_kpis_for_level: list[str] = []
            for t in metric_types:
                full_kpis_for_level.extend(full_by_type.get(t, []))
            # Deduplicate while preserving order a bit
            seen = set()
            full_kpis_for_level = [
                k for k in full_kpis_for_level if not (k in seen or seen.add(k))
            ]

            if core_kpis or full_kpis_for_level:
                parts: list[str] = []
                if core_kpis:
                    parts.append(
                        f"Core KPIs for level '{level}': {', '.join(core_kpis)}."
                    )
                if full_kpis_for_level:
                    parts.append(
                        "From the telemetry tables, non-dead KPIs available for this "
                        f"component type include (examples): {', '.join(full_kpis_for_level[:25])}. "
                        "When you call execute(), use these KPI names as column filters or features "
                        "(treat them as metric column names, not file paths)."
                    )
                kpi_hint = " ".join(parts)

                # Log the KPI catalog actually used for this deep dive, so we can verify
                # that the right KPIs are being exposed per component/level.
                sample_core = ", ".join(core_kpis[:20]) if core_kpis else ""
                sample_full = ", ".join(full_kpis_for_level[:40]) if full_kpis_for_level else ""
                msg = (
                    f"[Deep Dive] KPI catalog for component={node.component!r}, level={level!r}, "
                    f"dataset={self.profile.name!r}. metric_types={metric_types}. "
                    f"core_kpis_sample=[{sample_core}] full_kpis_sample=[{sample_full}]"
                )
                self._log_service(msg)
        except Exception:
            kpi_hint = ""
        # Root cause classes (reasons) this component can have
        possible_reasons = list(
            self.profile.reasons_by_level.get(level, [])
            or self.profile.possible_reasons
            or []
        )
        if not possible_reasons and self.profile.possible_reasons:
            possible_reasons = list(self.profile.possible_reasons)
        time_window_neg = self._shift_time(node.time, -5)
        time_window_pos = self._shift_time(node.time, 5)
        window_str = f"[{time_window_neg or 't-5'} , {time_window_pos or 't+5'}]"

        # Full query window (dataset-level) for comparing periodic vs episodic anomalies.
        query_window_str = ""
        try:
            if self.time_range and "start" in self.time_range and "end" in self.time_range:
                start_ts = float(self.time_range["start"])
                end_ts = float(self.time_range["end"])
                qs = datetime.utcfromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M:%S")
                qe = datetime.utcfromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M:%S")
                query_window_str = f"[{qs} , {qe}]"
        except Exception:
            query_window_str = ""

        # Explicit list for the prompt (possible root cause classes)
        possible_reasons_list = "\n".join(
            f"- {r}" for r in possible_reasons
        ) if possible_reasons else "(none — use low confidence and omit reason to prune)"

        anomalous_kpi = getattr(node, "kpi", None) or ""
        background = (
            f"Target candidate: component={node.component!r}, "
            f"fault time={node.time!r}. "
        )
        if anomalous_kpi:
            background += (
                f"The KPI that showed an anomaly in localization was: {anomalous_kpi!r}. "
                f"Localization reason_hint={node.reason!r}. "
            )
        else:
            background += f"Localization reason_hint={node.reason!r}. "
        bg_parts = [
            f"Namespace for all actions: {self.namespace!r}. ",
            f"Time window for root cause: {window_str} (narrow down to one root cause class in this window). ",
        ]
        if query_window_str:
            bg_parts.append(
                f"Full query window for baseline/periodicity checks: {query_window_str}. "
            )
        bg_parts.append(f"Component level (node/pod/service): {level!r}. ")
        bg_parts.append(f"{kpi_hint}")
        background += "".join(bg_parts)
        # Telecom-specific system knowledge for docker (pod-level) deep dives.
        dataset_name = (self.profile.name or "").replace("openrca_", "")
        if dataset_name.startswith("telecom") and level == "pod":
            background += (
                " In this Telecom system, components are layered as follows:\n"
                "- os_001~022: OS nodes that host DB/services. Network delay/loss is mainly visible "
                "via ICMP_ping, Sent_queue, Received_queue, and sometimes Disk_io_util / Memory_used_pct "
                "when the node is under pressure.\n"
                "- docker_001~004: first-tier service containers (frontend layer) running on os_021/022.\n"
                "- docker_005~008: second-tier backend containers that talk to db_001~013 via JDBC.\n"
                "- db_001~013: Oracle DB instances; Proc_Used_Pct, Sess_Connect, Proc_User_Used_Pct, "
                "Session_pct, On_Off_State, and tnsping_result_time indicate DB connection/close faults.\n"
                "- redis_*: middleware/cache (see metric_middleware).\n\n"
                "Trace callType semantics for network diagnosis:\n"
                "- CSF: caller-side framework/network-facing elapsed time.\n"
                "- RemoteProcess: callee-side remote processing elapsed time.\n"
                "- To estimate caller-callee network gap, do NOT pair by same cmdb_id only. "
                "Use call-chain linkage first (pid/id parent-child relation with same traceId), "
                "then compute network_gap = elapsedTime(CSF) - elapsedTime(RemoteProcess).\n\n"
                "When you deep-dive a docker_* component, treat generic trace latency on that "
                "docker as a **symptom** unless you can rule out CPU/DB causes:\n"
                "- First, check container CPU metrics (e.g. container_cpu_used) on that docker for "
                "sustained spikes or saturation.\n"
                "- Second, check the corresponding db_*** KPIs (Proc_Used_Pct, Sess_Connect, "
                "Proc_User_Used_Pct, Session_pct, On_Off_State, tnsping_result_time) for connection "
                "limits, DB pressure, latency, or closes.\n"
                "- Use ICMP_ping, Sent_queue, Received_queue, Memory_used_pct, and Disk_io_util on os_*** "
                "plus trace error rate to decide whether the underlying "
                "network is actually degraded.\n"
                "Only choose 'network delay' as the root cause when there is no plausible CPU/DB "
                "fault that can explain the latency."
            )
        elif dataset_name.startswith("market"):
            background += (
                " Market trace status semantics: in trace_span.csv, treat status_code values "
                "`0`, `OK`, `Ok`, `200`, and `SUCCESS` as success (non-error). "
                "When computing error_rate, do NOT count status `0` as an error. "
                "Use a normalized boolean like `is_error = NOT(status IN {0, OK, Ok, 200, SUCCESS})`. "
            )
            if level == "pod":
                background += (
                    "Market pod metric naming note: in metric_container tables, cmdb_id may be "
                    "'node-X.<pod_name>' while traces/logs typically use '<pod_name>'. "
                    "When running execute() filters for this component, match both exact pod name and "
                    "suffix-after-dot forms to avoid false 'no data' conclusions."
                )
        if possible_reasons:
            background += (
                f"Root cause classes you must choose from — pick exactly one that best matches "
                f"(and if possible occurred earliest in the window): {', '.join(possible_reasons)}. "
            )
        background += (
            "Use multiple KPIs and execute() as needed to determine which root cause class actually caused the problem; then conclude with one reason or low confidence to prune."
        )
        actions_desc = self.problem.get_available_actions() or {}
        expand_actions_desc = {
            name: doc for name, doc in actions_desc.items()
            if name in ("execute", "submit")
        }
        action_list = "\n".join(
            f"  - {name}: {doc[:200]}" for name, doc in expand_actions_desc.items()
        )
        system = _DEEP_DIVE_SYSTEM_TEMPLATE.format(
            possible_reasons_list=possible_reasons_list,
            background=background,
            workflow=_DEEP_DIVE_WORKFLOW,
            action_list=action_list or "(none)",
        )
        initial = (
            f"Deep-dive this candidate: component={node.component!r}, "
            f"time={node.time!r}, time window={window_str}. "
        )
        if anomalous_kpi:
            initial += f"The KPI that showed anomaly in localization: {anomalous_kpi!r}. "
        initial += (
            f"Use actions (multiple KPIs, execute as needed) to narrow down to which root cause class from the list best matches. "
            "You may prune by returning low confidence if no reason fits. "
            "Respond with JSON only: thought, action, args; when concluding add confidence, reason (one from the list), explanation, other_suspect."
        )
        parser = ResponseParser()
        verdict, messages = run_controller_stage(
            stage_name="deep_dive",
            system_prompt=system,
            initial_user_message=initial,
            problem=self.problem,
            llm_configs=self.configs,
            parser=parser,
            max_steps=15,
            sprint=self.sprint,
            response_format="react_json",
            component_level=level,
        )
        conf = 0.0
        explanation = ""
        verdict_reason: str | None = None
        other_suspect: list[str] = []
        if verdict:
            conf = float(verdict.get("confidence", 0))
            explanation = str(verdict.get("explanation", ""))[:500]
            # Optional: controller can refine earliest anomaly start time for this component.
            # If a valid time is provided, normalize and update node.time so later stages
            # (including expand) use this refined timestamp instead of the localization time.
            new_time_raw = (verdict.get("time") or "").strip()
            if new_time_raw:
                normalized = self._normalize_outlier_time(new_time_raw)
                if normalized:
                    self._log_service(
                        f"[Deep Dive] Updated time for component={node.component!r} "
                        f"from localization time {node.time!r} to earliest anomaly "
                        f"time {normalized!r} based on deep-dive analysis."
                    )
                    node.time = normalized

            verdict_reason = verdict.get("reason")
            if isinstance(verdict_reason, str):
                verdict_reason = verdict_reason.strip()
            else:
                verdict_reason = None
            other_suspect = list(verdict.get("other_suspect", []))
            if other_suspect:
                self._log_service(f"[Deep Dive] other_suspect: {other_suspect}")
        # Validate: reason must be one of the allowed root cause classes
        if verdict_reason and possible_reasons:
            if verdict_reason not in possible_reasons:
                # Try case-insensitive or exact substring match
                normalized = None
                for r in possible_reasons:
                    if r.lower() == (verdict_reason or "").lower():
                        normalized = r
                        break
                if normalized is not None:
                    verdict_reason = normalized
                    self._log_service(
                        f"[Deep Dive] Normalized reason to allowed class: {verdict_reason!r}"
                    )
                else:
                    self._log_service(
                        f"[Deep Dive] Rejected reason (not in allowed list): {verdict_reason!r}. "
                        f"Allowed: {possible_reasons}. Using localization reason_hint or omitting."
                    )
                    verdict_reason = None
        elif verdict_reason and not possible_reasons:
            verdict_reason = None
        # reason_hint stays as-is; store allowed class as deep-dive class.
        final_reason = verdict_reason or node.reason
        reason_class = (verdict_reason or self._normalize_reason_to_class(node.reason)) or None
        self._upsert_deep_dive_on_node(
            node,
            reason=final_reason,
            reason_class=reason_class,
            confidence=conf,
            explanation=explanation,
            checked_reasons=(
                [{"reason": final_reason, "confidence": round(float(conf), 4), "explanation": explanation[:280]}]
                if final_reason else []
            ),
            time_str=node.time,
        )
        # Low confidence → do not pass to next stage.
        if conf < 0.50:
            return []
        return [cid]

    def _stage3_controller_expand(
        self, hypothesis_ids: list[str], candidate_ids: list[str],
    ) -> list[str]:
        """Expand: use controller with history to find related/root-cause components.
        Uses graph.py topology (call/deployment/shared-resource) to suggest related
        components in addition to controller verdict.
        """
        if not hypothesis_ids or not self.problem:
            return []
        best = self.tree.nodes[hypothesis_ids[0]]
        trace_related, trace_relation_map = self._get_trace_relation_candidates(
            best.component,
            anchor_time=best.time,
            window_min=5,
        )
        deploy_related, deploy_relation_map, deploy_groups = (
            self._get_deployment_relation_candidates(best.component)
        )
        topology_related, topology_relation_map = self._get_topology_relation_candidates(
            best.component
        )
        relation_meta_map = self._merge_relation_candidate_maps(
            trace_relation_map,
            deploy_relation_map,
            topology_relation_map,
        )
        graph_related = sorted(relation_meta_map.keys())
        logger.info(
            "[Expand] %s @ %s | trace=%d deploy=%d topology=%d merged=%d",
            best.component,
            best.time,
            len(trace_related),
            len(deploy_related),
            len(topology_related),
            len(graph_related),
        )
        if trace_related:
            logger.info("[Expand] %s trace candidates: %s", best.component, trace_related)
        if deploy_related:
            logger.info("[Expand] %s deployment candidates: %s", best.component, deploy_related)
        if topology_related:
            logger.info("[Expand] %s topology candidates: %s", best.component, topology_related)
        if graph_related:
            logger.info("[Expand] %s merged dependency candidates: %s", best.component, graph_related)
        rel_map: dict[str, list[str]] = {}
        for comp, items in relation_meta_map.items():
            display_parts: list[str] = []
            seen: set[str] = set()
            for item in items:
                relation_type = str(item.get("relation_type") or "").strip()
                label = str(item.get("label") or "").strip()
                piece = relation_type if not label else f"{relation_type}/{label}"
                if piece and piece not in seen:
                    seen.add(piece)
                    display_parts.append(piece)
            rel_map[comp] = display_parts
        if graph_related:
            # Basic list
            self._log_service(
                f"[Expand] Relation-based expand candidates for {best.component!r}: "
                f"{graph_related[:15]}{'...' if len(graph_related) > 15 else ''}"
            )
            # Relation detail per candidate (how it is linked to the hypothesis)
            annotated = []
            for comp in graph_related:
                rels = rel_map.get(comp) or []
                if rels:
                    annotated.append(f"{comp} [{' / '.join(rels)}]")
                else:
                    annotated.append(f"{comp} [unknown]")
            self._log_service(
                "[Expand] Relation types per candidate: " + "; ".join(annotated)
            )
        # Full query window (dataset-level) for comparing periodic vs episodic anomalies.
        query_window_str = ""
        try:
            if self.time_range and "start" in self.time_range and "end" in self.time_range:
                start_ts = float(self.time_range["start"])
                end_ts = float(self.time_range["end"])
                qs = datetime.utcfromtimestamp(start_ts).strftime("%Y-%m-%d %H:%M:%S")
                qe = datetime.utcfromtimestamp(end_ts).strftime("%Y-%m-%d %H:%M:%S")
                query_window_str = f"[{qs} , {qe}]"
        except Exception:
            query_window_str = ""

        actions_desc = self.problem.get_available_actions() or {}
        action_list = "\n".join(
            f"  - {name}: {doc[:200]}" for name, doc in actions_desc.items()
        )
        if not graph_related:
            logger.info(
                "[Expand] %s produced 0 dependency candidates before anomaly filtering",
                best.component,
            )
            return []

        # Split relation-based candidates into two passes:
        #   1) Trace anomaly edges (caller/callee) → first expand pass
        #      This includes:
        #        - direct trace_call neighbours of the current hypothesis, and
        #        - any deployment-contained components that appear in global trace anomaly edges.
        #   2) Topology/deployment/shared-resource dependencies from graph.py → second pass
        trace_first: set[str] = set()
        for comp, infos in (relation_meta_map or {}).items():
            if any(
                (info.get("relation_family") or "").strip() == "trace_call"
                and self._is_time_within_minutes(best.time, str(info.get("time") or "").strip(), 5)
                for info in infos or []
            ):
                trace_first.add(comp)

        grouped_trace: dict[str, list[str]] = {}
        grouped_graph: dict[str, list[str]] = {}
        for comp in graph_related:
            group_key = self._get_expand_group_key(comp)
            if comp in trace_first:
                grouped_trace.setdefault(group_key, []).append(comp)
            else:
                grouped_graph.setdefault(group_key, []).append(comp)

        if grouped_trace or grouped_graph:
            parts: list[str] = []
            if grouped_trace:
                parts.append(
                    "trace_edges="
                    + ", ".join(
                        f"{self._format_expand_group_label(group)}={len(comps)}"
                        for group, comps in grouped_trace.items()
                    )
                )
            if grouped_graph:
                parts.append(
                    "graph_dep="
                    + ", ".join(
                        f"{self._format_expand_group_label(group)}={len(comps)}"
                        for group, comps in grouped_graph.items()
                    )
                )
            self._log_service("[Expand] Grouped candidate batches: " + " ; ".join(parts))

        class _LockedProblemProxy:
            def __init__(self, problem, lock):
                self._problem = problem
                self._lock = lock

            def get_available_actions(self):
                return self._problem.get_available_actions()

            def perform_action(self, action_name, *args, **kwargs):
                with self._lock:
                    return self._problem.perform_action(action_name, *args, **kwargs)

        locked_problem = _LockedProblemProxy(self.problem, self._controller_action_lock)

        def _run_expand_group(group_key: str, group_candidates: list[str]) -> list[dict]:
            group_label = self._format_expand_group_label(group_key)
            cached_components: list[dict] = []
            fresh_candidates: list[str] = []
            for comp in group_candidates:
                cached = self._get_expand_cached_verdict(comp, best.time)
                if cached is None:
                    fresh_candidates.append(comp)
                    continue
                cached["_reused_from_cache"] = True
                cached_components.append(cached)
                self._log_service(
                    f"[Expand] Reusing cached verdict for {comp!r} in {group_label} batch "
                    f"around time {best.time!r}: has_anomaly={cached.get('has_anomaly')!r}, "
                    f"value_is_problematic={cached.get('value_is_problematic')!r}"
                )
            if not fresh_candidates:
                self._log_service(
                    f"[Expand] Skipped {group_label} batch LLM call: all {len(group_candidates)} "
                    "candidates already inspected in a nearby window."
                )
                return cached_components
            background = (
                f"Current hypothesis: component={best.component!r}, reason={best.reason!r}, time={best.time!r}. "
                f"Namespace: {self.namespace!r}. "
                + (f"Full query window for baseline/periodicity checks: {query_window_str}. " if query_window_str else "")
            )
            annotated_bg: list[str] = []
            for comp in fresh_candidates:
                rels = rel_map.get(comp) or []
                if rels:
                    annotated_bg.append(f"{comp} ({'/'.join(rels)})")
                else:
                    annotated_bg.append(comp)
            background += (
                f"This expand batch contains ONLY {group_label} candidates. "
                "The following components are relation-based candidates for expand. "
                "Trace-based caller/callee edges are precomputed from anomalous edge behavior "
                "(error rate, network gap, or remote process time) in the query window, while deployment "
                "relations come from the deployment graph. Relation types in parentheses show "
                "how each candidate is linked to the current hypothesis: "
                f"{', '.join(annotated_bg)}. "
            )
            group_kpis = self._get_expand_group_kpis(group_key)
            if group_kpis:
                background += (
                    f"For this {group_label} batch, inspect these {group_label}-related KPIs as relevant "
                    f"before concluding: {', '.join(group_kpis)}. "
                    "Do not stop after checking only one representative KPI if multiple KPIs are available "
                    "for this component family. "
                )
            if (self.profile.name or "").replace("openrca_", "").startswith("telecom"):
                if group_key == "docker":
                    background += (
                        "Telecom Docker KPI semantics: judge `container_cpu_used` by absolute level, not only by relative lift. "
                        "A rise from near 0 to around 20 can be anomalous versus baseline but is usually NOT operationally problematic by itself. "
                        "Use `value_is_problematic=true` only when the absolute CPU level is clearly high/sustained or when additional evidence shows real CPU stress. "
                        "Likewise, modest memory increases should not be treated as problematic unless the absolute level or sustained rise is meaningfully high. "
                    )
                elif group_key == "db":
                    background += (
                        "Telecom DB KPI semantics: for `Session_pct`, `Sess_Connect`, `Proc_Used_Pct`, `Proc_User_Used_Pct`, and `tnsping_result_time`, "
                        "judge `value_is_problematic` by true saturation, pressure, or latency level, not only by peer gap. "
                        "A brief or moderate rise that stays away from operational limits should usually be false. "
                    )
                elif group_key == "os":
                    background += (
                        "Telecom OS KPI semantics: for `ICMP_ping`, `Sent_queue`, `Received_queue`, `Disk_io_util`, and `Memory_used_pct`, "
                        "judge `value_is_problematic` by substantial sustained latency, backlog, or pressure. Tiny or brief changes should usually be false. "
                    )
            if (self.profile.name or "").replace("openrca_", "").startswith("market"):
                background += (
                    "Market trace status semantics: in trace_span.csv, treat status_code values "
                    "`0`, `OK`, `Ok`, `200`, and `SUCCESS` as success (non-error). "
                    "When computing error_rate, do NOT count status `0` as an error. "
                    "Use a normalized boolean like `is_error = NOT(status IN {0, OK, Ok, 200, SUCCESS})`. "
                )
                if group_label == "pod":
                    background += (
                        "Market pod naming note: metric_container `cmdb_id` can be `node-X.<pod_name>` while traces/logs use `<pod_name>`. "
                        "In execute() analyses, filter pods using both exact and suffix-after-dot matches. "
                    )
            background += (
                "During expand you should treat only this batch as the candidate set and, using execute() only, "
                "inspect each candidate's own KPIs in the t-5min to t+5min window around the hypothesis time "
                "to see which ones show strong anomalies worth advancing to later stages, "
                "and which ones only show propagated effects or weak evidence. Do not decide the final root cause here; "
                "return only the filtered anomaly candidates from this batch."
            )

            system = _EXPAND_SYSTEM_TEMPLATE.format(
                background=background,
                workflow=_EXPAND_WORKFLOW,
                action_list=action_list or "(none)",
            )
            initial = (
                f"Expand batch [{group_label}] from hypothesis: component={best.component!r}, "
                f"reason={best.reason!r}, time={best.time!r}. "
                f"Candidates in this batch: {fresh_candidates}. "
                "Follow the workflow. Respond with JSON only: thought, action, args; when concluding use "
                "action=submit with a 'components' list where each entry includes component, time, has_anomaly, confidence, "
                "anomalous_kpi, anomaly_value, value_is_problematic, value_judgment, and clues."
            )
            self._log_service(
                f"[Expand] Starting {group_label} batch with candidates: {fresh_candidates}"
            )
            parser = ResponseParser()
            verdict, _messages = run_controller_stage(
                stage_name="expand",
                system_prompt=system,
                initial_user_message=initial,
                problem=locked_problem,
                llm_configs=self.configs,
                parser=parser,
                max_steps=15,
                sprint=self.sprint,
                response_format="react_json",
            )
            components = verdict.get("components") if verdict else None
            if not isinstance(components, list):
                components = []
            allowed = set(fresh_candidates)
            components = [
                entry for entry in components
                if isinstance(entry, dict)
                and (entry.get("component") or "").strip() in allowed
            ]
            for entry in components:
                self._store_expand_cached_verdict(entry, best.time)
            self._log_service(
                f"[Expand] Finished {group_label} batch: {len(components)} new component verdict(s), "
                f"{len(cached_components)} reused from cache"
            )
            return cached_components + components

        def _run_grouped_batches(grouped_related: dict[str, list[str]]) -> list[dict]:
            """Run one or more expand batches for a grouped candidate map."""
            if not grouped_related:
                return []
            out: list[dict] = []
            if len(grouped_related) == 1:
                group_key, group_candidates = next(iter(grouped_related.items()))
                out.extend(_run_expand_group(group_key, group_candidates))
            else:
                with ThreadPoolExecutor(max_workers=min(3, len(grouped_related))) as executor:
                    futures = {
                        executor.submit(_run_expand_group, group_key, group_candidates): group_key
                        for group_key, group_candidates in grouped_related.items()
                    }
                    for future in as_completed(futures):
                        group_key = futures[future]
                        try:
                            out.extend(future.result())
                        except Exception as e:
                            self._log_service(
                                f"[Expand] {self._format_expand_group_label(group_key)} batch failed: {e}"
                            )
            return out

        new_candidate_ids: list[str] = []
        components_info: list[dict] = []
        skipped_not_anomaly = 0
        skipped_not_problematic = 0
        pruned_low_conf = 0
        skipped_duplicate = 0
        skipped_self = 0

        # 1) First pass: components connected via anomalous trace edges.
        #    These skip the controller/execute loop and are treated as confirmed
        #    expand candidates based solely on trace anomaly evidence. We also
        #    attach detailed latency/error/volume change info so that later
        #    stages (including global judge) can reason over concrete metric
        #    deltas instead of a generic "anomaly" tag.
        for comp in sorted(trace_first):
            rel_infos = relation_meta_map.get(comp) or []
            trace_infos = [
                info
                for info in rel_infos
                if (info.get("relation_family") or "").strip() == "trace_call"
            ]
            # If this component was promoted to trace_first only via deployment
            # membership (no local trace_call relation_infos), fall back to the
            # global _trace_anomaly_edges cache to synthesize a trace entry.
            if not trace_infos and getattr(self, "_trace_anomaly_edges", None):
                synthetic: list[dict] = []
                for edge in self._trace_anomaly_edges:
                    caller = str(edge.get("caller") or "").strip()
                    callee = str(edge.get("callee") or "").strip()
                    edge_time = str(edge.get("time") or "").strip()
                    if not self._is_time_within_minutes(best.time, edge_time, 5):
                        continue
                    if comp not in (caller, callee):
                        continue
                    anomaly_type = str(edge.get("anomaly_type") or "mixed").strip() or "mixed"
                    time_str = edge_time
                    synthetic.append(
                        {
                            "relation_family": "trace_call",
                            "relation_type": "call_downstream" if comp == caller else "call_upstream",
                            "anomaly_type": anomaly_type,
                            "time": time_str,
                            "end_time": str(edge.get("end_time") or "").strip(),
                            "duration_minutes": edge.get("duration_minutes"),
                            "label": self._format_trace_relation_badge(anomaly_type, time_str),
                            "metadata": {
                                "edge_id": edge.get("edge_id"),
                                "caller": caller,
                                "callee": callee,
                                **(edge.get("metadata") or {}),
                            },
                        }
                    )
                trace_infos = synthetic
            if not trace_infos:
                continue
            # Choose nearest anomaly time among trace edges for this component.
            trace_infos_sorted = sorted(
                trace_infos,
                key=lambda x: (
                    0 if self._is_time_within_minutes(best.time, str(x.get("time") or "").strip(), 5) else 1,
                    str(x.get("time") or ""),
                    str(x.get("anomaly_type") or ""),
                ),
            )
            primary = trace_infos_sorted[0]
            t_str = str(primary.get("time") or "").strip() or best.time
            anomaly_type = str(primary.get("anomaly_type") or "mixed").strip() or "mixed"
            meta = primary.get("metadata") or {}
            event = meta.get("event") or {}
            error_info = meta.get("error_info") or {}
            gap_info = meta.get("gap_info") or {}
            remote_info = meta.get("remote_info") or {}
            detail_parts: list[str] = []
            if error_info:
                try:
                    err_base = error_info.get("baseline")
                    err_peak = error_info.get("peak")
                    err_thr = error_info.get("threshold")
                    detail_parts.append(f"error_rate {err_base}→{err_peak} (thr={err_thr})")
                except Exception:
                    pass
            if gap_info:
                try:
                    gap_base = gap_info.get("baseline")
                    gap_peak = gap_info.get("peak")
                    gap_thr = gap_info.get("threshold")
                    detail_parts.append(f"network_gap {gap_base}→{gap_peak} (thr={gap_thr})")
                except Exception:
                    pass
            if remote_info:
                try:
                    rem_base = remote_info.get("baseline")
                    rem_peak = remote_info.get("peak")
                    rem_thr = remote_info.get("threshold")
                    detail_parts.append(f"remote_process_time {rem_base}→{rem_peak} (thr={rem_thr})")
                except Exception:
                    pass
            if event:
                detail_parts.append(
                    f"episode {event.get('onset')}~{event.get('end_time')} ({event.get('duration_minutes')}m)"
                )
            detail_str = "; ".join(detail_parts)
            kpi_name = {
                "error_rate": "error_rate",
                "network_gap": "network_gap",
                "remote_process_time": "remote_process_time",
            }.get(anomaly_type, f"trace_{anomaly_type}")
            components_info.append(
                {
                    "component": comp,
                    "time": t_str,
                    "has_anomaly": True,
                    "_auto_trace_confirmed": True,
                    "anomalous_kpi": kpi_name,
                    "anomaly_value": detail_str,
                    "value_is_problematic": True,
                    "value_judgment": "precomputed trace edge anomaly",
                    "confidence": 1.0,
                    "clues": (
                        f"trace_edge:{anomaly_type}@{t_str} | {detail_str}"
                        if detail_str
                        else f"trace_edge:{anomaly_type}@{t_str}"
                    ),
                }
            )
            self._log_service(
                f"[Expand] Auto-confirmed trace-edge dependency for {comp!r} at {t_str!r} "
                f"(anomaly_type={anomaly_type!r}) without controller/execute."
            )

        # 2) Second pass: remaining topology/deployment/shared-resource dependencies
        #    go through the controller/execute-based expand workflow.
        components_info.extend(_run_grouped_batches(grouped_graph))

        # Precompute localization candidates (component, time) to preserve low-confidence
        # dependencies that were already surfaced in Stage 1 (within ±5 minutes).
        localize_nodes = [
            n for n in self.tree.nodes.values()
            if n.stage == "localize" and (n.time or "").strip()
        ]

        def _get_localized_match(
            component: str,
            time_str: str | None,
            window_min: float = 5.0,
        ):
            if not component or not time_str:
                return None
            try:
                t_new = datetime.strptime(str(time_str).strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
            best_match = None
            best_key = None
            for ln in localize_nodes:
                if ln.component != component or not (ln.time or "").strip():
                    continue
                try:
                    t_loc = datetime.strptime(str(ln.time).strip(), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                delta_sec = abs((t_new - t_loc).total_seconds())
                if delta_sec > window_min * 60.0:
                    continue
                match_key = (-float(getattr(ln, "severity", 0.0) or 0.0), delta_sec)
                if best_key is None or match_key < best_key:
                    best_key = match_key
                    best_match = ln
            return best_match

        to_add: list[tuple[str, str, str | None, float, str, object | None]] = []
        existing_children_under_parent = {
            (
                (n.parent_id or "").strip(),
                (n.component or "").strip(),
            )
            for n in self.tree.nodes.values()
            if (n.component or "").strip()
        }

        # Controller returns per-component anomaly summaries; only components with
        # both has_anomaly == true and value_is_problematic == true will be turned
        # into new expand candidates.
        for entry in components_info:
            if not isinstance(entry, dict):
                continue
            comp = (entry.get("component") or "").strip()
            if not comp or comp == best.component:
                skipped_self += 1
                continue
            if (hypothesis_ids[0], comp) in existing_children_under_parent:
                # Avoid creating the exact same child twice under the same parent,
                # but allow the same component to appear under other parents so the
                # expand tree can show multiple plausible causal paths.
                skipped_duplicate += 1
                continue
            has_anom = entry.get(
                "has_anomaly",
                True if entry.get("_auto_trace_confirmed") else False,
            )
            if isinstance(has_anom, str):
                has_anom = has_anom.strip().lower() in {"true", "yes", "1"}
            if not has_anom:
                skipped_not_anomaly += 1
                continue
            t_str = entry.get("time") or best.time
            clues = entry.get("clues") or ""
            anomalous_kpi = (entry.get("anomalous_kpi") or "").strip()
            anomaly_value = (entry.get("anomaly_value") or "").strip()
            value_is_problematic = entry.get("value_is_problematic")
            if isinstance(value_is_problematic, str):
                value_is_problematic = value_is_problematic.strip().lower() in {
                    "true", "yes", "1", "problematic"
                }
            if value_is_problematic is not True:
                self._log_service(
                    f"[Expand] Skipped dependency {comp!r} at {t_str!r}: "
                    f"has_anomaly={has_anom!r}, "
                    f"value_is_problematic={value_is_problematic!r}"
                )
                skipped_not_problematic += 1
                continue
            value_judgment = (entry.get("value_judgment") or "").strip()
            detail_parts: list[str] = []
            if anomalous_kpi:
                detail_parts.append(f"kpi={anomalous_kpi}")
            if anomaly_value:
                detail_parts.append(f"value={anomaly_value}")
            if value_is_problematic is not None:
                detail_parts.append(f"value_is_problematic={value_is_problematic}")
            if value_judgment:
                detail_parts.append(f"value_judgment={value_judgment}")
            if clues:
                detail_parts.append(f"clues={clues}")
            evidence = " | ".join(detail_parts) if detail_parts else clues
            rel_infos = relation_meta_map.get(comp) or []
            rel_str = self._relation_label_from_infos(rel_infos)
            # Optional per-component anomaly confidence from controller (0.0–1.0)
            comp_conf = entry.get("confidence")
            try:
                comp_conf = float(comp_conf) if comp_conf is not None else 0.0
            except (TypeError, ValueError):
                comp_conf = 0.0
            localized_match = _get_localized_match(comp, t_str)
            if localized_match is not None:
                loc_time = (localized_match.time or "").strip()
                loc_sev = float(getattr(localized_match, "severity", 0.0) or 0.0)
                detail_parts.append(
                    f"localized_hit={loc_time or 'unknown'}"
                )
                if loc_sev > 0:
                    detail_parts.append(f"localized_severity={loc_sev:.0f}")
                evidence = " | ".join(detail_parts)
            else:
                evidence = " | ".join(detail_parts) if detail_parts else clues
            # Prune: keep only components with sufficiently high anomaly confidence,
            # or ones that were already discovered in localization (within ±5min).
            if comp_conf <= 0.5 and localized_match is None:
                self._log_service(
                    f"[Expand] Pruned dependency {comp!r} at {t_str!r}: "
                    f"has_anomaly={has_anom!r}, confidence={comp_conf:.2f} "
                    "(not in localization within ±5min)."
                )
                pruned_low_conf += 1
                continue
            to_add.append((comp, t_str, rel_str, comp_conf, evidence, localized_match))

        # Summary: how many topology dependencies vs how many anomaly candidates kept.
        total_dep = len(set(graph_related or []))
        kept = len(to_add)
        # Summary: keep a compact "X/Y" ratio for visualization, and log a
        # more descriptive message to session.log.
        ratio_str = f"{kept}/{total_dep}" if total_dep > 0 else f"{kept}/0"
        summary = f"Expand anomalies kept: {ratio_str} dependency candidates"
        if total_dep > 0:
            self._log_service(f"[Expand] {summary}")
        logger.info(
            "[Expand] %s summary | deps=%d verdicts=%d kept=%d skipped_no_anomaly=%d "
            "skipped_not_problematic=%d pruned_low_conf=%d skipped_duplicate=%d",
            best.component,
            total_dep,
            len(components_info),
            kept,
            skipped_not_anomaly,
            skipped_not_problematic,
            pruned_low_conf,
            skipped_duplicate,
        )
        if kept == 0 and total_dep > 0:
            logger.info(
                "[Expand] %s kept 0/%d dependencies after filtering. Check session.log for per-component verdict details.",
                best.component,
                total_dep,
            )

        for comp, t_str, rel_str, comp_conf, evidence, localized_match in to_add:
            rel_infos = relation_meta_map.get(comp) or []
            rc_id = self.tree.add_candidate(
                stage="expand",
                component=comp,
                time_str=t_str,
                parent_id=hypothesis_ids[0],
                evidence=evidence or ratio_str,
                relation=rel_str,
                confidence=comp_conf,
                localized_match=localized_match is not None,
                localized_time=(localized_match.time if localized_match is not None else None),
                localized_severity=(
                    float(getattr(localized_match, "severity", 0.0) or 0.0)
                    if localized_match is not None else 0.0
                ),
            )
            self._attach_expand_relation_edges(
                hypothesis_ids[0],
                best.component,
                rc_id,
                comp,
                rel_infos,
            )
            new_candidate_ids.append(rc_id)
            self._log_service(
                f"  + New candidate from expand: {comp} "
                f"(time={t_str!r}, confidence={comp_conf:.2f}, relation={rel_str!r}, evidence={evidence!r})"
            )
            self._update_live_view()
        self._register_deployment_containment_for_expand(
            hypothesis_ids[0],
            best.component,
            new_candidate_ids,
            deploy_groups,
        )
        if deploy_groups:
            self._update_live_view()
        return new_candidate_ids

    # ── Stage 2: Deep Dive (fixed pipeline) ───────────────────────────

    def _upsert_deep_dive_on_node(
        self,
        node: TreeNode,
        *,
        reason: str | None,
        reason_class: str | None,
        confidence: float,
        explanation: str,
        checked_reasons: list[dict] | None = None,
        time_str: str | None = None,
    ) -> None:
        """Store deep-dive result on the existing node instead of creating a child node."""
        if time_str:
            node.time = time_str
        node.deep_dive_reason = reason or node.deep_dive_reason
        node.deep_dive_reason_class = reason_class or node.deep_dive_reason_class
        node.deep_dive_time = node.time
        node.deep_dive_confidence = max(
            float(getattr(node, "deep_dive_confidence", 0.0) or 0.0),
            float(confidence or 0.0),
        )
        if explanation:
            node.deep_dive_evidence = str(explanation)[:1200]
        if checked_reasons is not None:
            node.deep_dive_checked_reasons = list(checked_reasons)[:16]

        # Keep deep-dive conclusion as authoritative class, but keep reason hint if already present.
        if reason_class:
            node.root_cause_reason_class = reason_class
        if confidence >= 0.50:
            node.confidence = max(float(getattr(node, "confidence", 0.0) or 0.0), float(confidence))
            if reason:
                node.reason = reason

        # Log a lightweight event in tree timeline for visibility.
        self.tree._step += 1
        self.tree._record(
            "deep_dive_update",
            node.id,
            confidence=float(confidence or 0.0),
            reason=(reason_class or reason or ""),
        )
        self._update_live_view()

    def stage2_deep_dive(self, candidate_ids: list[str]) -> list[str]:
        """For each candidate, verify possible reasons via execute()."""
        hypothesis_ids: list[str] = []

        for cid in candidate_ids:
            node = self.tree.nodes[cid]
            level = self._get_level(node.component)
            reasons = list(self.profile.reasons_by_level.get(level, []))

            # Prioritize stage1 reason hint
            if node.reason and node.reason in reasons:
                reasons = [node.reason] + [
                    r for r in reasons if r != node.reason
                ]

            reason_results: list[dict] = []
            best_reason: str | None = None
            best_conf = -1.0
            best_explanation = ""
            for reason in reasons:
                instruction = (
                    f"Check if component '{node.component}' shows "
                    f"'{reason}' around {node.time}. "
                    f"Load the relevant metric CSV and compare "
                    f"'{node.component}' against peers in a ±5 min window. "
                    f"Report whether the signal is present and how strong."
                )
                self._log_agent(
                    f"[Deep Dive] execute: {node.component} × {reason} @ {node.time}"
                )
                try:
                    evidence = self.actions.execute(instruction)
                except Exception as e:
                    evidence = f"execute() error: {e}"

                self._log_service(
                    f"[Deep Dive] execute result ({node.component}/{reason}):\n"
                    f"{evidence[:500]}"
                )

                conf, explanation = self._score_confidence(
                    node.component, reason, node.time or "", evidence,
                )
                reason_results.append(
                    {
                        "reason": reason,
                        "confidence": round(float(conf), 4),
                        "explanation": str(explanation)[:280],
                    }
                )
                if conf > best_conf:
                    best_conf = conf
                    best_reason = reason
                    best_explanation = explanation

            if best_reason is None:
                continue
            best_reason_class = self._normalize_reason_to_class(best_reason) or best_reason
            self._upsert_deep_dive_on_node(
                node,
                reason=best_reason,
                reason_class=best_reason_class,
                confidence=max(0.0, best_conf),
                explanation=best_explanation,
                checked_reasons=reason_results,
                time_str=node.time,
            )

            if best_conf < 0.70:
                self._log_service(
                    f"  ✗ Deep-dive low confidence: {node.component}/{best_reason} conf={best_conf:.2f}"
                )
            else:
                hypothesis_ids.append(cid)
                self._log_service(
                    f"  ✓ Deep-dive updated node: {node.component}/{best_reason} "
                    f"conf={best_conf:.2f} — {best_explanation[:200]}"
                )

        # Order hypotheses by confidence (highest first) so that Stage 3 and
        # subsequent iterations always consider stronger hypotheses earlier.
        hypothesis_ids.sort(
            key=lambda hid: (
                float(getattr(self.tree.nodes[hid], "deep_dive_confidence", 0.0) or 0.0)
                if hid in self.tree.nodes else 0.0
            ),
            reverse=True,
        )
        return hypothesis_ids

    def _score_confidence(
        self, component: str, reason: str, time_str: str, evidence: str,
    ) -> tuple[float, str]:
        prompt = _DEEP_DIVE_PROMPT.format(
            component=component, reason=reason,
            time=time_str, evidence=evidence[:3000],
        )
        self._log_agent(
            f"[Deep Dive] Scoring confidence: {component}/{reason}"
        )
        try:
            raw = get_chat_completion(
                [{"role": "user", "content": prompt}],
                self.configs, temperature=0.0,
            )
            self._log_service(f"[Deep Dive] LLM confidence response:\n{raw}")
            parsed = json.loads(self._extract_json(raw))
            conf = float(parsed.get("confidence", 0))
            expl = parsed.get("explanation", "")
            return conf, expl
        except Exception as e:
            self._log_service(f"[Deep Dive] Confidence scoring failed: {e}")
            return 0.0, str(e)

    # ── Stage 3: Expand ──────────────────────────────────────────────

    def stage3_expand(self, hypothesis_ids: list[str]) -> list[str]:
        """Check time-shifted windows, dependent components, and graph topology."""
        new_candidate_ids: list[str] = []

        for hid in hypothesis_ids:
            node = self.tree.nodes[hid]
            # Graph-based related components (call/deployment/shared-resource)
            graph_related = get_related_components_for_expand(
                node.component, self.profile.name
            )
            if graph_related:
                self._log_service(
                    f"[Expand] Graph-related for {node.component!r}: "
                    f"{graph_related[:10]}{'...' if len(graph_related) > 10 else ''}"
                )
            expanded_set = {node.component}
            self._log_agent(
                f"[Expand] {node.component}/{node.reason} — "
                f"checking t-5, t-10 windows"
            )

            for offset in [-5, -10]:
                verdict = self._check_time_shifted(node, offset)
                if not verdict:
                    continue

                child_id = self.tree.add_candidate(
                    stage="expand",
                    component=node.component,
                    reason=node.reason,
                    time_str=self._shift_time(node.time, offset),
                    parent_id=hid,
                    evidence=verdict.get("explanation", ""),
                    confidence=verdict.get("confidence", 0),
                )
                self._update_live_view()

                v = verdict.get("verdict", "unclear")
                if v == "root_cause":
                    self.tree.confirm(
                        child_id, verdict.get("confidence", 0.8),
                        f"Anomaly precedes fault (t{offset}min)",
                    )
                    self._update_live_view()
                    self._log_service(
                        f"  ✓ root_cause at t{offset}min: "
                        f"{node.component}/{node.reason}"
                    )
                elif v == "symptom":
                    self.tree.prune(child_id, f"Symptom at t{offset}min")
                    self.tree.prune(hid, "Determined to be symptom")
                    self._update_live_view()
                    self._log_service(
                        f"  ✗ symptom at t{offset}min: "
                        f"{node.component}/{node.reason}"
                    )
                else:
                    self._log_service(
                        f"  ? unclear at t{offset}min: "
                        f"{node.component}/{node.reason}"
                    )

                verdict_related = verdict.get("related_components", []) or []
                for comp in verdict_related:
                    if comp and comp != node.component:
                        rc_id = self.tree.add_candidate(
                            stage="expand",
                            component=comp,
                            time_str=node.time,
                            parent_id=hid,
                            evidence=(
                                f"Discovered via {node.component} expansion"
                            ),
                        )
                        new_candidate_ids.append(rc_id)
                        self._log_service(
                            f"  + New candidate from expansion: {comp}"
                        )
                        self._update_live_view()
                        expanded_set.add(comp)
            for comp in graph_related:
                if comp and comp not in expanded_set:
                    expanded_set.add(comp)
                    rc_id = self.tree.add_candidate(
                        stage="expand",
                        component=comp,
                        time_str=node.time,
                        parent_id=hid,
                        evidence=(
                            "Discovered via graph topology (call/deployment/shared-resource)"
                        ),
                    )
                    new_candidate_ids.append(rc_id)
                    self._log_service(
                        f"  + New candidate from graph: {comp}"
                    )
                    self._update_live_view()

        return new_candidate_ids

    def _check_time_shifted(
        self, node: TreeNode, offset_min: int,
    ) -> dict | None:
        shifted_time = self._shift_time(node.time, offset_min)
        if not shifted_time:
            return None

        instruction = (
            f"Check component '{node.component}' at time {shifted_time} "
            f"(offset {offset_min}min from original {node.time}). "
            f"Is the anomaly '{node.reason}' already present? "
            f"Also list any OTHER components showing anomalies "
            f"related to '{node.component}'."
        )
        self._log_agent(
            f"[Expand] execute: {node.component} @ t{offset_min}min ({shifted_time})"
        )
        try:
            shifted_evidence = self.actions.execute(instruction)
        except Exception as e:
            self._log_service(f"[Expand] Time-shift execute failed: {e}")
            return None

        self._log_service(
            f"[Expand] execute result (t{offset_min}min):\n"
            f"{shifted_evidence[:500]}"
        )

        prompt = _EXPAND_PROMPT.format(
            component=node.component, reason=node.reason,
            time=node.time, offset=offset_min,
            shifted_evidence=shifted_evidence[:2000],
            original_evidence=node.evidence[:1000],
        )
        try:
            raw = get_chat_completion(
                [{"role": "user", "content": prompt}],
                self.configs, temperature=0.0,
            )
            self._log_service(f"[Expand] LLM verdict:\n{raw}")
            return json.loads(self._extract_json(raw))
        except Exception as e:
            self._log_service(f"[Expand] LLM failed: {e}")
            return None

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_image_paths(text: str) -> list[str]:
        return [
            line.strip() for line in text.splitlines()
            if line.strip().endswith(".png") and os.path.isfile(line.strip())
        ]

    @staticmethod
    def _extract_visual_summary_text(
        text: str,
        *,
        max_lines: int = 40,
        max_chars: int = 4000,
    ) -> str:
        if not text:
            return ""
        lines: list[str] = []
        for raw in str(text).splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.endswith(".png") and os.path.isfile(line):
                continue
            lines.append(line)
            if len(lines) >= max_lines:
                break
        out = "\n".join(lines)
        if len(out) > max_chars:
            out = out[:max_chars].rstrip() + "\n...[truncated]"
        return out

    @staticmethod
    def _encode_images(paths: list[str]) -> list[dict]:
        blocks = []
        for p in paths:
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        return blocks

    def _call_vision_critic(
        self, image_paths: list[str], context: str = "", prompt_override: str | None = None,
    ) -> str:
        image_blocks = self._encode_images(image_paths)
        prompt = prompt_override if prompt_override else self._critic_prompt
        content: list[dict] = [
            {"type": "text", "text": prompt},
        ]
        content.extend(image_blocks)
        self._log_agent(f"[Vision Critic] Analyzing {context} ({len(image_paths)} image(s))")
        try:
            result = get_chat_completion(
                [{"role": "user", "content": content}], self.configs,
            )
            self._log_service(f"[Vision Critic] [{context}] Response:\n{result}")
            return result or ""
        except Exception as e:
            self._log_service(f"[Vision Critic] [{context}] Failed: {e}")
            return ""

    def _normalize_outlier_time(self, time_str: str | None) -> str | None:
        """Normalize outlier time to YYYY-MM-DD HH:MM:SS using query window date when available.
        If the critic returns a full datetime (e.g. 2026-03-11 05:03:00), we replace the date part
        with the query date (e.g. 2022-03-20) so times stay in the query's time range.
        """
        if not time_str or not time_str.strip():
            return None
        s = time_str.strip()
        # Strip common prefixes that break parsing
        for prefix in ("approx ", "approximately ", "~", "around ", "at "):
            if s.lower().startswith(prefix):
                s = s[len(prefix):].strip()
        s = re.sub(r"\s*(UTC|Z)$", "", s, flags=re.I)

        # Query date for this problem (from time_range); use for both full-datetime and time-only.
        query_date_str = None
        if self.time_range and "start" in self.time_range:
            try:
                start_ts = float(self.time_range["start"])
                dt = datetime.utcfromtimestamp(start_ts)
                query_date_str = dt.strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                pass

        # Already full datetime: keep time part but replace date with query date when available
        try:
            datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            if query_date_str:
                time_part = s.split(None, 1)[1] if " " in s else s  # "05:03:00"
                return f"{query_date_str} {time_part}"
            return s
        except ValueError:
            pass
        try:
            datetime.strptime(s, "%Y-%m-%d %H:%M")
            if query_date_str:
                time_part = s.split(None, 1)[1] if " " in s else s
                return f"{query_date_str} {time_part}:00" if len(time_part) <= 8 else f"{query_date_str} {time_part}"
            return s + ":00" if len(s) == 16 else s
        except ValueError:
            pass

        # Time-only: need date from query window. If we don't know the query date,
        # we can't safely normalize, so return None.
        if not query_date_str:
            return None
        date_str = query_date_str
        # Parse HH:MM:SS or HH:MM (2-digit hour)
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                t = datetime.strptime(s, fmt)
                return f"{date_str} {t.strftime('%H:%M:%S')}"
            except ValueError:
                continue
        # Regex fallback: 1–2 digit hour, e.g. 2:22:00 or 02:22
        m = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*", s)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            sec = int(m.group(3)) if m.group(3) else 0
            if 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= sec <= 59:
                return f"{date_str} {h:02d}:{mi:02d}:{sec:02d}"
        return None

    def _parse_trace_path_outliers(self, critic_text: str) -> list[dict]:
        """Parse vision critic output for trace anomalous path chart (multiple candidates)."""
        try:
            raw_json = self._extract_json(critic_text)
            parsed = json.loads(raw_json)
            outliers = parsed.get("outliers", [])
            result: list[dict] = []
            seen: set[tuple[str, str, str]] = set()
            for o in outliers:
                if not isinstance(o, dict):
                    continue
                comp_name = (o.get("component") or "").strip()
                anomalous_kpi = str(o.get("anomalous_kpi") or "").strip().lower()
                if anomalous_kpi not in {"error_rate", "network_gap", "remote_process_time"}:
                    change = str(o.get("change") or "").lower()
                    if "gap" in change:
                        anomalous_kpi = "network_gap"
                    elif "remote" in change:
                        anomalous_kpi = "remote_process_time"
                    else:
                        anomalous_kpi = "error_rate"
                comp_name = self._map_component_by_trace_kpi(comp_name, anomalous_kpi)
                normalized_time = self._normalize_outlier_time(o.get("time")) or str(o.get("time") or "")
                dedup_key = (comp_name, anomalous_kpi, normalized_time)
                if not comp_name or dedup_key in seen:
                    continue
                seen.add(dedup_key)
                sev_raw = o.get("severity", 0)
                try:
                    severity = float(sev_raw)
                except (TypeError, ValueError):
                    severity = 70.0
                if severity < 50.0:
                    continue
                value_is_problematic = o.get("value_is_problematic")
                if isinstance(value_is_problematic, str):
                    value_is_problematic = (
                        value_is_problematic.strip().lower()
                        in {"true", "yes", "1", "problematic"}
                    )
                if value_is_problematic is False and severity < 70.0:
                    continue
                result.append({
                    "component": comp_name,
                    "change": o.get("change", "trace_path anomaly"),
                    "severity": severity,
                    "time": normalized_time,
                    "reason_hint": o.get("reason_hint"),
                    "anomalous_kpi": anomalous_kpi,
                    "anomaly_value": o.get("anomaly_value"),
                })
            return result
        except Exception as e:
            self._log_service(f"Failed to parse trace path outliers: {e}")
            return []

    def _parse_outliers(
        self, critic_text: str, kpi: str, *, include_filtered_aux: bool = False
    ) -> list[dict] | tuple[list[dict], list[dict]]:
        try:
            raw_json = self._extract_json(critic_text)
            parsed = json.loads(raw_json)
            outliers = parsed.get("outliers", [])
            result: list[dict] = []
            filtered_aux: list[dict] = []
            seen_components: set[tuple[str, str]] = set()
            for o in outliers:
                if not isinstance(o, dict):
                    continue
                comp_name = o.get("component", "unknown")
                kpi_val = o.get("kpi") or kpi
                reason_hint = o.get("reason_hint")
                if not reason_hint:
                    reasons = self.profile.kpi_to_reasons.get(kpi, [])
                    reason_hint = reasons[0] if reasons else None
                aux_base = {
                    "component": comp_name,
                    "kpi": kpi_val,
                    "time": self._normalize_outlier_time(o.get("time")) or o.get("time"),
                    "change": o.get("change", ""),
                    "duration_approx": o.get("duration_approx"),
                    "anomaly_value": o.get("anomaly_value"),
                    "anomaly_type": o.get("anomaly_type"),
                    "severity": o.get("severity"),
                    "reason_hint": reason_hint,
                    "value_is_problematic": o.get("value_is_problematic"),
                    "value_judgment": o.get("value_judgment"),
                }
                # Skip clearly repeating/cyclical patterns
                if (o.get("anomaly_type") or "").lower() == "cyclic":
                    if include_filtered_aux:
                        filtered_aux.append({**aux_base, "drop_reason": "cyclic"})
                    continue
                key = (comp_name, kpi_val)
                # Drop repeated outliers for the same (component, kpi) to avoid
                # reporting multiple similar spikes/drops on a repeating pattern.
                if key in seen_components:
                    if include_filtered_aux:
                        filtered_aux.append({**aux_base, "drop_reason": "duplicate_component_kpi"})
                    continue
                seen_components.add(key)
                sev_raw = o.get("severity", 0)
                try:
                    severity = float(sev_raw)
                except (TypeError, ValueError):
                    # Fallback mapping from labels if the model still uses them
                    label = str(sev_raw).lower()
                    if "high" in label:
                        severity = 80.0
                    elif "medium" in label:
                        severity = 50.0
                    elif "low" in label:
                        severity = 20.0
                    else:
                        severity = 0.0
                # Drop weak outliers (severity <= 70)
                if severity <= 70.0:
                    if include_filtered_aux:
                        filtered_aux.append({**aux_base, "drop_reason": "low_severity"})
                    continue
                value_is_problematic = o.get("value_is_problematic")
                if isinstance(value_is_problematic, str):
                    value_is_problematic = value_is_problematic.strip().lower() in {
                        "true", "yes", "1", "problematic"
                    }
                if value_is_problematic is False:
                    if include_filtered_aux:
                        filtered_aux.append({**aux_base, "drop_reason": "not_problematic"})
                    continue
                normalized_time = self._normalize_outlier_time(o.get("time"))
                result.append({
                    "component": comp_name,
                    "change": o.get("change", ""),
                    "severity": severity,
                    "time": normalized_time or o.get("time"),
                    "reason_hint": reason_hint,
                    "kpi": kpi_val,
                    "duration_approx": o.get("duration_approx"),
                    "anomaly_value": o.get("anomaly_value"),
                    "anomaly_type": o.get("anomaly_type"),
                    "value_is_problematic": value_is_problematic,
                    "value_judgment": o.get("value_judgment"),
                })
            if include_filtered_aux:
                return result, filtered_aux
            return result
        except Exception as e:
            self._log_service(f"Failed to parse outliers: {e}")
            return ([], []) if include_filtered_aux else []

    def _filter_outliers_by_self_baseline(
        self, outliers: list[dict], kpi: str, comp_type: str,
    ) -> list[dict]:
        """Numeric gate to suppress false positives from tiny level shifts.

        Criteria (per outlier component):
          - robust z-score vs its own baseline (MAD-based), OR
          - relative median change,
          - and persistence in fault window.
        """
        if not outliers:
            return outliers
        try:
            df = self.actions.static_app.fetch_metrics_df(
                self.namespace,
                start_time=self.time_range.get("start") if self.time_range else None,
                end_time=self.time_range.get("end") if self.time_range else None,
            )
        except Exception as e:
            self._log_service(f"[{comp_type}/{kpi}] baseline gate skipped (fetch failed): {e}")
            return outliers

        if df.empty:
            return outliers

        # Normalize schema
        if "name" in df.columns and "kpi_name" not in df.columns:
            df = df.rename(columns={"name": "kpi_name"})
        if "startTime" in df.columns and "timestamp" not in df.columns:
            df = df.rename(columns={"startTime": "timestamp"})
        if "kpi_name" not in df.columns or "cmdb_id" not in df.columns or "timestamp" not in df.columns or "value" not in df.columns:
            return outliers

        kpi_df = df[df["kpi_name"] == kpi].copy()
        if kpi_df.empty:
            return outliers

        ts = kpi_df["timestamp"].astype(float)
        if ts.median() > 1e12:
            ts = ts / 1000.0
        kpi_df["_ts"] = ts

        kept: list[dict] = []
        for o in outliers:
            comp = str(o.get("component") or "").strip()
            if not comp:
                continue
            # Support both exact and suffix forms (e.g. node-1.checkoutservice-0)
            comp_mask = (kpi_df["cmdb_id"] == comp) | (
                kpi_df["cmdb_id"].astype(str).str.split(".", n=1).str[-1] == comp
            )
            cdf = kpi_df[comp_mask].sort_values("_ts")
            if cdf.empty:
                continue

            t_str = o.get("time")
            if not t_str:
                kept.append(o)
                continue
            try:
                event_ts = float(
                    datetime.strptime(str(t_str), "%Y-%m-%d %H:%M:%S")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except Exception:
                kept.append(o)
                continue

            # Windows around detected time
            baseline = cdf[(cdf["_ts"] >= event_ts - 8 * 60) & (cdf["_ts"] < event_ts - 2 * 60)]["value"].astype(float)
            fault = cdf[(cdf["_ts"] >= event_ts - 1 * 60) & (cdf["_ts"] <= event_ts + 2 * 60)]["value"].astype(float)
            if baseline.empty or fault.empty:
                kept.append(o)
                continue

            b_med = float(baseline.median())
            f_med = float(fault.median())
            abs_delta = abs(f_med - b_med)
            rel_delta = abs_delta / max(abs(b_med), 1e-6)

            b_mad = float((baseline - b_med).abs().median())
            std_equiv = b_mad * 1.4826 if b_mad > 1e-9 else 0.0
            if std_equiv > 1e-9:
                robust_z = abs(f_med - b_med) / std_equiv
            else:
                robust_z = 0.0 if abs_delta < 1e-9 else rel_delta * 10.0

            # persistence: how much of fault window is consistently shifted from baseline
            shift_thr = max(2.0 * std_equiv, max(abs(b_med) * 0.03, 0.5))
            persist_ratio = float((fault - b_med).abs().ge(shift_thr).mean())

            # KPI-aware gate: first check that the absolute KPI level is problematic,
            # then apply generic statistical constraints. This prevents cases where
            # a tiny level shift (e.g., a few MB of mem) is treated as an outlier.
            problematic = True
            kpi_lower = (kpi or "").lower()
            dataset = (self.profile.name or "").replace("openrca_", "")
            if dataset.startswith("telecom"):
                # Docker container memory: require at least ~15% jump and 2+ units shift
                if "container_mem" in kpi_lower:
                    problematic = rel_delta >= 0.15 and abs_delta >= 2.0
                # Docker CPU: either reach high utilization or a strong relative jump
                elif "container_cpu" in kpi_lower:
                    problematic = (f_med >= 70.0) or (rel_delta >= 0.20 and f_med >= 50.0)
                # DB session percentage: treat only high saturation as problematic
                elif "session_pct" in kpi_lower:
                    problematic = f_med >= 60.0

            passes = problematic and (robust_z >= 2.5 or rel_delta >= 0.10) and persist_ratio >= 0.60
            if passes:
                kept.append(o)
            else:
                self._log_service(
                    f"[{comp_type}/{kpi}] dropped outlier {comp!r}: "
                    f"rel={rel_delta:.3f}, z={robust_z:.2f}, persist={persist_ratio:.2f}, "
                    f"b_med={b_med:.3f}, f_med={f_med:.3f}"
                )

        return kept

    @staticmethod
    def _extract_json(text: str) -> str:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            return m.group(1)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return m.group(0) if m else "{}"

    def _get_level(self, component: str) -> str:
        """Infer logical level ("node", "pod", "service") from profile.component_levels.

        Prefer the explicit mapping from the static-dataset config (component_levels).
        Many datasets (e.g. Market) use bare names in component_levels (checkoutservice-2),
        while metrics/visualization may use prefixed forms like node-2.checkoutservice-2.
        We therefore try:
          1) Exact match against each level's component list
          2) Suffix match after '.' (e.g. node-2.checkoutservice-2 → checkoutservice-2)
        Only if both fail do we fall back to simple heuristics.
        """
        comp = component or ""
        # 1) Exact match
        for level, components in self.profile.component_levels.items():
            if comp in components:
                return level
        # 2) Suffix match for forms like "node-2.checkoutservice-2"
        if "." in comp:
            suffix = comp.split(".", 1)[1]
            for level, components in self.profile.component_levels.items():
                if suffix in components:
                    return level
        # 3) Fallback heuristics (best-effort only)
        if comp.startswith("node-") or comp.startswith("os_"):
            return "node"
        if comp.startswith("docker_"):
            return "pod"
        if comp.startswith("db_"):
            return "service"
        return "service"

    def _get_expand_group_key(self, component: str) -> str:
        """Return the component-family key used to batch expand filtering."""
        comp = (component or "").strip()
        dataset = (self.profile.name or "").replace("openrca_", "")
        if dataset.startswith("telecom"):
            if comp.startswith("db_"):
                return "db"
            if comp.startswith("docker_"):
                return "docker"
            if comp.startswith("os_"):
                return "os"
        return self._get_level(comp) or "other"

    @staticmethod
    def _format_expand_group_label(group_key: str) -> str:
        return {
            "db": "DB",
            "docker": "Docker",
            "os": "OS",
            "node": "node",
            "pod": "pod",
            "service": "service",
            "other": "other",
        }.get(group_key, group_key)

    def _get_expand_group_kpis(self, group_key: str) -> list[str]:
        """Return the KPI checklist to inject into expand prompts for a group."""
        dataset = (self.profile.name or "").replace("openrca_", "")
        if dataset.startswith("telecom"):
            if group_key == "db":
                return (
                    self.profile.full_kpis_by_type.get("service")
                    or self.profile.kpis_by_type.get("db")
                    or []
                )
            if group_key == "docker":
                return (
                    self.profile.full_kpis_by_type.get("container")
                    or self.profile.kpis_by_type.get("docker")
                    or []
                )
            if group_key == "os":
                return (
                    self.profile.full_kpis_by_type.get("node")
                    or self.profile.kpis_by_type.get("os")
                    or []
                )
        return (
            self.profile.full_kpis_by_type.get(group_key)
            or self.profile.kpis_by_type.get(group_key)
            or []
        )

    def _normalize_reason_to_class(self, reason: str | None) -> str:
        """Return reason iff it is one of the allowed root cause classes (exact or case-insensitive)."""
        if not reason or not (reason := reason.strip()):
            return ""
        possible = self.profile.possible_reasons or []
        if not possible:
            return reason
        if reason in possible:
            return reason
        for r in possible:
            if r.lower() == reason.lower():
                return r
        return ""

    def _is_valid_prediction(self, node: TreeNode) -> bool:
        """Validate final prediction against dataset root cause schema and query window."""
        # 1) Component class constraint
        if self.profile.possible_components:
            if node.component not in self.profile.possible_components:
                self._log_service(
                    f"Rejecting prediction: component {node.component!r} not in possible_components"
                )
                return False

        # 2) Reason class constraint
        if self.profile.possible_reasons:
            rc_class = getattr(node, "root_cause_reason_class", None) or ""
            reason_val = (rc_class or node.reason or "").strip()
            if reason_val not in self.profile.possible_reasons:
                self._log_service(
                    f"Rejecting prediction: reason {node.reason!r} / root_cause_reason_class {rc_class!r} not in possible_reasons"
                )
                return False

        return True

    @staticmethod
    def _shift_time(time_str: str | None, offset_min: int) -> str | None:
        if not time_str:
            return None
        try:
            dt = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
            shifted = dt + timedelta(minutes=offset_min)
            return shifted.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @staticmethod
    def _time_minutes_epoch(time_str: str | None) -> float | None:
        """Parse time string (UTC) to minutes since epoch for comparison. Returns None if missing/invalid."""
        if not time_str or not time_str.strip():
            return None
        try:
            dt = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.timestamp() / 60.0
        except ValueError:
            return None

    def _get_expand_cached_verdict(
        self, component: str, time_str: str | None, window_min: float = 5.0
    ) -> dict | None:
        """Return the closest cached expand verdict for this component near time_str."""
        comp = (component or "").strip()
        if not comp:
            return None
        target_min = self._time_minutes_epoch(time_str)
        with self._expand_cache_lock:
            candidates = list(self._expand_verdict_cache.get(comp, []))
        if not candidates:
            return None
        if target_min is None:
            return dict(candidates[0])

        best_entry = None
        best_delta = None
        for entry in candidates:
            cached_min = self._time_minutes_epoch(entry.get("_window_time"))
            if cached_min is None:
                continue
            delta = abs(cached_min - target_min)
            if delta > window_min:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_entry = entry
        return dict(best_entry) if best_entry is not None else None

    def _store_expand_cached_verdict(
        self, entry: dict, window_time: str | None, window_min: float = 5.0
    ) -> None:
        """Cache an expand verdict so nearby future parents can reuse it."""
        comp = (entry.get("component") or "").strip()
        if not comp:
            return
        cached = dict(entry)
        cached["_window_time"] = cached.get("time") or window_time
        cached["_cached_from_expand"] = True
        target_min = self._time_minutes_epoch(cached.get("_window_time"))
        with self._expand_cache_lock:
            bucket = self._expand_verdict_cache.setdefault(comp, [])
            if target_min is None:
                bucket.append(cached)
                return
            for idx, existing in enumerate(bucket):
                existing_min = self._time_minutes_epoch(existing.get("_window_time"))
                if existing_min is None:
                    continue
                if abs(existing_min - target_min) <= window_min:
                    bucket[idx] = cached
                    return
            bucket.append(cached)

    def _is_duplicate_candidate(
        self, nid: str, existing_ids: list[str], window_min: float = 5.0
    ) -> bool:
        """True if node nid is duplicate of any in existing_ids: same component, reason, and time within window_min minutes."""
        node = self.tree.nodes.get(nid)
        if not node:
            return False
        t_min = self._time_minutes_epoch(node.time)
        for eid in existing_ids:
            other = self.tree.nodes.get(eid)
            if not other:
                continue
            if node.component != other.component or node.reason != other.reason:
                continue
            t_min_other = self._time_minutes_epoch(other.time)
            if t_min is None and t_min_other is None:
                return True
            if t_min is not None and t_min_other is not None:
                if abs(t_min - t_min_other) <= window_min:
                    return True
        return False

    def _get_kpi_problem_hint(self, comp_type: str, kpi: str) -> str:
        """Return a short KPI-semantics hint for localization vision critic."""
        dataset = (self.profile.name or "").replace("openrca_", "")
        kpi_lower = (kpi or "").lower()
        if "fs_reads" in kpi_lower or "fs_writes" in kpi_lower or "io.r" in kpi_lower or "io.w" in kpi_lower:
            return (
                "KPI meaning hint: disk/file-system I/O spikes should NOT be treated as harmless by default "
                "just because separate saturation or latency evidence is absent. A large or clearly unusual "
                "read/write burst can still be operationally meaningful when it is strong, abrupt, or aligned "
                "in time with service behavior changes. Judge by the actual magnitude, abruptness, duration, "
                "and whether the burst plausibly explains the incident, not only by explicit proof of disk saturation."
            )
        if dataset.startswith("telecom"):
            if "container_cpu" in kpi_lower:
                return (
                    "KPI meaning hint: container CPU is problematic mainly when utilization is "
                    "clearly elevated/sustained or jumps strongly enough to indicate CPU stress. "
                    "Small peer differences at moderate levels are usually not serious."
                )
            if "container_mem" in kpi_lower:
                return (
                    "KPI meaning hint: container memory is problematic only when the increase is "
                    "meaningful in absolute or relative terms; tiny step changes should not be "
                    "treated as serious."
                )
            if "icmp_ping" in kpi_lower:
                return (
                    "KPI meaning hint: ICMP_ping is problematic when latency spikes clearly, "
                    "or when packet loss/network instability is implied. Small jitter is not serious."
                )
            if "received_queue" in kpi_lower or "sent_queue" in kpi_lower:
                return (
                    "KPI meaning hint: Received_queue/Sent_queue indicate backlog or queue buildup mainly "
                    "when the queue becomes clearly elevated or sustained. Very small absolute values such as "
                    "around 1-10 are usually NOT operationally problematic by themselves, even if a peer stays at 0. "
                )
            if "session_pct" in kpi_lower:
                return (
                    "KPI meaning hint: Session_pct is problematic mainly when it rises toward "
                    "high saturation / connection exhaustion, not just because peers differ."
                )
            if "sess_connect" in kpi_lower:
                return (
                    "KPI meaning hint: Sess_Connect is problematic when sessions surge toward a limit "
                    "or collapse unexpectedly; small peer gaps alone are not enough."
                )
            if "on_off_state" in kpi_lower:
                return (
                    "KPI meaning hint: On_Off_State is problematic when it shows a true service down/"
                    "state transition, not a visually minor fluctuation."
                )
        return (
            f"KPI meaning hint: judge whether the actual value of {comp_type}/{kpi} is operationally "
            "problematic, not just visually different."
        )
