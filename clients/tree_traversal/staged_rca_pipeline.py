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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from aiopslab.orchestrator.parser import ResponseParser
from aiopslab.orchestrator.static_actions.trace_path_renderer import (
    get_trace_path_renderer,
)

from clients.openrca_rca.api_router import get_chat_completion
from clients.tree_traversal.controller_stage import run_controller_stage
from clients.tree_traversal.deep_dive_stage import run_deep_dive_controller
from clients.tree_traversal.dataset_profile import DatasetProfile
from clients.tree_traversal.trace_expand_stage import run_trace_expand_controller
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

- Pick an existing candidate from the tree by `node_id`. Do not invent a new candidate.
- Pick an existing candidate from the tree. Do not invent a new candidate.
- Prefer a candidate whose **own KPI evidence** looks operationally problematic, not just visually different.
- Prefer candidates that can **explain downstream anomalies** on their descendants or related branches.
- Do not over-prefer generic symptom nodes if there is a deeper KPI-based cause such as CPU, DB, network, disk, queue, or service-down evidence.
- Use topology/path information, relation types, localization severity, and expand evidence together.
- If cross-candidate relations/topology evidence are weak or inconclusive, prioritize KPI semantics and absolute operational impact over topology links.
- In that case, prefer candidates with stronger problematic-value evidence (`value_is_problematic`, `value_judgment`, `anomaly_value`, persistence) rather than the largest single relative spike.
- If deep-dive evidence is present (either dedicated deep-dive nodes or node-level deep_dive_* fields), treat it as stronger per-candidate evidence than raw reason hints.
- Respect causal direction hints from trace-expand:
  - upstream_cause > downstream_effect > ambiguous for root-cause selection.
  - ambiguous nodes may stay as follow-up candidates but should not be preferred as final root cause unless no stronger candidate exists.
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
  "node_id": "<optional: existing node id from the tree>",
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
- If decision is "final", fill component/reason/time/confidence and optionally node_id/supporting_path.
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
        enable_deep_dive: bool = True,        # whether Stage 2 per-node deep dive runs
        enable_trace_path_vision_localize: bool = False,  # Stage1 trace_anomalous_path vision critic
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
        self.enable_trace_path_vision_localize = bool(enable_trace_path_vision_localize)
        self.expand_max_hops = max(1, min(expand_max_hops, 2))
        self.live_viewer = live_viewer
        self._controller_action_lock = Lock()
        self._expand_cache_lock = Lock()
        self._expand_verdict_cache: dict[str, list[dict]] = {}
        self._trace_anomaly_edges: list[dict] = []
        self._expand_component_existing_count: dict[str, int] = {}
        self._last_expand_stop_due_existing_count: bool = False
        self._last_expand_stop_components: list[str] = []
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
            dd_verdict = str(getattr(node, "deep_dive_verdict", "") or "").strip().lower()
            if dd_verdict == "noise":
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
                    "level": self._get_level(node.component),
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
                    "deep_dive_verdict": getattr(node, "deep_dive_verdict", None),
                    "deep_dive_confidence": float(getattr(node, "deep_dive_confidence", 0.0) or 0.0),
                    "deep_dive_confidence_avg": float(
                        getattr(node, "deep_dive_confidence_avg", 0.0) or 0.0
                    ),
                    "deep_dive_confidence_count": int(
                        getattr(node, "deep_dive_confidence_count", 0) or 0
                    ),
                    "deep_dive_time": getattr(node, "deep_dive_time", None),
                    "deep_dive_evidence": (getattr(node, "deep_dive_evidence", "") or "")[:600],
                    "deep_dive_checked_reasons": list(getattr(node, "deep_dive_checked_reasons", []) or [])[:8],
                    "deep_dive_next_kpis": list(getattr(node, "deep_dive_next_kpis", []) or [])[:12],
                    "deep_dive_edge_targets": list(getattr(node, "deep_dive_edge_targets", []) or [])[:8],
                    "related_parent_components": list(getattr(node, "related_parent_components", []) or [])[:8],
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
        # Add service->pod containment groups from graph.py deployment_graph.
        # This helps global judge reason about service-level vs pod-level consistency
        # using explicit dataset topology, not heuristic alias inference.
        try:
            node_comp_map: dict[str, str] = {
                str(item.get("id") or "").strip(): str(item.get("component") or "").strip()
                for item in nodes_payload
                if str(item.get("id") or "").strip() and str(item.get("component") or "").strip()
            }
            comp_to_node_ids: dict[str, list[str]] = {}
            for nid, comp in node_comp_map.items():
                comp_to_node_ids.setdefault(comp, []).append(nid)

            graphs = get_graphs(self._get_dependency_graph_dataset_key())
            deployment_graph = graphs.get("deployment_graph") or {}
            service_to_members: dict[str, list[str]] = {}
            for container_comp, members in deployment_graph.items():
                service = str(container_comp or "").strip()
                if not service or self._get_level(service) != "service":
                    continue
                mids: list[str] = []
                for member_comp in (members or []):
                    member = str(member_comp or "").strip()
                    if not member or self._get_level(member) != "pod":
                        continue
                    for nid in comp_to_node_ids.get(member, []):
                        if nid not in mids:
                            mids.append(nid)
                if mids:
                    service_to_members[service] = mids

            existing_keys: set[tuple[str, tuple[str, ...], str]] = set()
            for g in containment_groups_payload:
                cc = str(g.get("container_component") or "").strip()
                mids = tuple(sorted(str(x).strip() for x in (g.get("member_node_ids") or []) if str(x).strip()))
                rf = str(g.get("relation_family") or "").strip()
                existing_keys.add((cc, mids, rf))

            for service, mids in service_to_members.items():
                member_ids = [x for x in dict.fromkeys(mids) if x in included_node_ids]
                if not member_ids:
                    continue
                key = (service, tuple(sorted(member_ids)), "service_contains")
                if key in existing_keys:
                    continue
                member_components = [
                    node_comp_map.get(mid, "")
                    for mid in member_ids
                    if node_comp_map.get(mid, "")
                ]
                label = f"{service} ({', '.join(member_components)})" if member_components else service
                containment_groups_payload.append(
                    {
                        "container_node_id": None,
                        "container_component": service,
                        "member_node_ids": member_ids,
                        "relation_family": "service_contains",
                        "relation_type": "contains",
                        "label": label,
                        "metadata": {"member_components": member_components},
                    }
                )
        except Exception:
            # Best-effort enrichment only; keep judge summary robust.
            pass

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
            try:
                prompt_path = self.save_dir / f"global_judge_input_attempt_{attempt + 1}.txt"
                prompt_payload = (
                    "[SYSTEM]\n"
                    + str(system or "")
                    + "\n\n[USER]\n"
                    + str(user_message or "")
                )
                prompt_path.write_text(prompt_payload, encoding="utf-8")
                self._log_service(f"[Global Judge] Prompt input saved to {prompt_path}")
            except Exception as e:
                self._log_service(f"[Global Judge] Failed to save prompt input: {e}")
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

            chosen_level = self._get_level(chosen.component)
            verdict_level = str(verdict.get("component_level") or "").strip().lower()
            if verdict_level in {"node", "pod", "service"} and verdict_level != chosen_level:
                self._log_service(
                    f"[Global Judge] Note: verdict.component_level={verdict_level!r} "
                    f"differs from inferred level={chosen_level!r} for component={chosen.component!r}."
                )
            if chosen_level == "node":
                impact_state, impacted_pods, hosted_pods, impact_ratio = self._has_broad_colocated_pod_impact(
                    chosen.component,
                    chosen.time,
                    window_min=10,
                    min_ratio=0.5,
                )
                if impact_state == "weak":
                    self._log_service(
                        f"[Global Judge] Node-vs-pod warning: chosen node={chosen.component!r} "
                        f"has weak broad pod impact (impacted_pods={impacted_pods!r}, "
                        f"hosted_pods={hosted_pods}, impact_ratio={impact_ratio:.3f}). "
                        "Keeping original global judge selection (re-evaluation disabled)."
                    )
                elif impact_state == "unknown":
                    self._log_service(
                        f"[Global Judge] Node-vs-pod impact unknown for node={chosen.component!r} "
                        "(host/deployment evidence unavailable or hosted_pods=0)."
                    )

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

    def _summarize_tree_for_expand(self, focus_node_id: str | None = None) -> str:
        lines: list[str] = []
        for node in sorted(self.tree.nodes.values(), key=lambda n: (n.step, n.id)):
            if node.id == "root":
                continue
            lines.append(
                f"- {node.id}: component={node.component}, stage={node.stage}, status={node.status}, "
                f"time={node.time}, parent={node.parent_id}, related_parents={getattr(node, 'related_parent_components', [])}, "
                f"reason={node.reason}, confidence={float(getattr(node, 'confidence', 0.0) or 0.0):.2f}, "
                f"deep_dive_verdict={getattr(node, 'deep_dive_verdict', None)}, "
                f"deep_dive_reason={getattr(node, 'deep_dive_reason_class', None)}"
                + (" <-- current hypothesis" if focus_node_id and node.id == focus_node_id else "")
            )
        if self.tree.relation_edges:
            lines.append("Relation edges:")
            for edge in self.tree.relation_edges[-24:]:
                lines.append(
                    f"- {edge.get('src_component')} -> {edge.get('dst_component')} "
                    f"[{edge.get('relation_family')}/{edge.get('relation_type')}] "
                    f"time={edge.get('time')}"
                )
        if self.tree.containment_groups:
            lines.append("Containment groups:")
            for group in self.tree.containment_groups[-16:]:
                lines.append(
                    f"- container={group.get('container_component')} members={group.get('member_node_ids')}"
                )
        return "\n".join(lines[:140]) if lines else "(tree empty)"

    def _build_causal_graph_snapshot(self) -> dict:
        """Build a lightweight causal-graph snapshot from current tree state."""
        nodes: list[dict] = []
        edges: list[dict] = []

        # Include nodes with anomaly evidence (deep-dive anomaly) and expand nodes.
        node_ids: set[str] = set()
        for node in self.tree.nodes.values():
            if node is None or node.id == "root":
                continue
            stage = str(getattr(node, "stage", "") or "").strip().lower()
            dd_verdict = str(getattr(node, "deep_dive_verdict", "") or "").strip().lower()
            if stage == "expand" or dd_verdict == "anomaly":
                node_ids.add(node.id)
                nodes.append(
                    {
                        "id": node.id,
                        "component": str(getattr(node, "component", "") or "").strip(),
                        "level": self._get_level(getattr(node, "component", "") or ""),
                        "stage": stage,
                        "time": str(getattr(node, "time", "") or "").strip(),
                        "deep_dive_verdict": dd_verdict or None,
                        "deep_dive_reason": str(
                            getattr(node, "deep_dive_reason_class", "") or ""
                        ).strip()
                        or None,
                        "deep_dive_confidence": float(
                            getattr(node, "deep_dive_confidence_avg", 0.0)
                            or getattr(node, "deep_dive_confidence", 0.0)
                            or 0.0
                        ),
                    }
                )

        # Parent-child edges among selected nodes.
        for node_id in list(node_ids):
            node = self.tree.nodes.get(node_id)
            if node is None:
                continue
            pid = str(getattr(node, "parent_id", "") or "").strip()
            if pid and pid in node_ids:
                edges.append(
                    {
                        "src_id": pid,
                        "dst_id": node_id,
                        "type": "tree_parent_child",
                    }
                )

        # Relation edges whose endpoints are present.
        for edge in getattr(self.tree, "relation_edges", []) or []:
            src = str(edge.get("src_node_id") or "").strip()
            dst = str(edge.get("dst_node_id") or "").strip()
            if not src or not dst:
                continue
            if src not in node_ids or dst not in node_ids:
                continue
            edges.append(
                {
                    "src_id": src,
                    "dst_id": dst,
                    "type": "relation",
                    "relation_family": str(edge.get("relation_family") or "").strip(),
                    "relation_type": str(edge.get("relation_type") or "").strip(),
                    "time": str(edge.get("time") or "").strip(),
                    "label": str(edge.get("label") or "").strip(),
                }
            )

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        }

    def _build_seed_graph_snapshot(self, seed_ids: list[str]) -> dict:
        """Build seed-only graph snapshot from deep-dive survivors."""
        seed_set = {str(x).strip() for x in (seed_ids or []) if str(x).strip()}
        nodes: list[dict] = []
        edges: list[dict] = []
        if not seed_set:
            return {
                "nodes": [],
                "edges": [],
                "stats": {"node_count": 0, "edge_count": 0},
            }

        for nid in seed_set:
            node = self.tree.nodes.get(nid)
            if node is None:
                continue
            nodes.append(
                {
                    "id": nid,
                    "component": str(getattr(node, "component", "") or "").strip(),
                    "level": self._get_level(getattr(node, "component", "") or ""),
                    "stage": str(getattr(node, "stage", "") or "").strip().lower(),
                    "time": str(getattr(node, "time", "") or "").strip(),
                    "deep_dive_verdict": str(getattr(node, "deep_dive_verdict", "") or "").strip().lower()
                    or None,
                    "deep_dive_reason": str(getattr(node, "deep_dive_reason_class", "") or "").strip()
                    or None,
                    "deep_dive_confidence": float(
                        getattr(node, "deep_dive_confidence_avg", 0.0)
                        or getattr(node, "deep_dive_confidence", 0.0)
                        or 0.0
                    ),
                }
            )

        # Relation edges among surviving seeds only.
        for edge in getattr(self.tree, "relation_edges", []) or []:
            src = str(edge.get("src_node_id") or "").strip()
            dst = str(edge.get("dst_node_id") or "").strip()
            if not src or not dst or src not in seed_set or dst not in seed_set:
                continue
            edges.append(
                {
                    "src_id": src,
                    "dst_id": dst,
                    "type": "relation",
                    "relation_family": str(edge.get("relation_family") or "").strip(),
                    "relation_type": str(edge.get("relation_type") or "").strip(),
                    "time": str(edge.get("time") or "").strip(),
                    "label": str(edge.get("label") or "").strip(),
                }
            )

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
            },
        }

    def _render_snapshot_png(self, payload: dict, out_path: Path, *, title: str) -> None:
        """Render compact graph PNG from snapshot payload."""
        try:
            import matplotlib.pyplot as plt
        except Exception:
            self._log_service(f"[Graph] matplotlib not available; skip PNG render: {out_path}")
            return

        nodes = [n for n in (payload.get("nodes") or []) if isinstance(n, dict)]
        edges = [e for e in (payload.get("edges") or []) if isinstance(e, dict)]
        if not nodes:
            fig, ax = plt.subplots(figsize=(8, 3))
            ax.text(0.5, 0.5, "No nodes", ha="center", va="center", fontsize=12, color="#666")
            ax.set_title(title, fontsize=11)
            ax.set_axis_off()
            fig.tight_layout()
            try:
                fig.savefig(out_path, dpi=160, bbox_inches="tight")
            finally:
                plt.close(fig)
            return

        by_id = {str(n.get("id") or "").strip(): n for n in nodes}
        ordered = sorted(
            nodes,
            key=lambda n: (
                str(n.get("time") or ""),
                str(n.get("stage") or ""),
                str(n.get("component") or ""),
                str(n.get("id") or ""),
            ),
        )

        y_map = {"localize": 2.0, "deep_dive": 1.3, "expand": 0.6}
        positions: dict[str, tuple[float, float]] = {}
        for idx, n in enumerate(ordered):
            nid = str(n.get("id") or "").strip()
            stage = str(n.get("stage") or "").strip().lower()
            x = float(idx)
            y = float(y_map.get(stage, 1.0))
            positions[nid] = (x, y)

        width = max(10.0, 0.9 * max(8, len(ordered)))
        fig, ax = plt.subplots(figsize=(width, 4.8))

        for e in edges:
            src = str(e.get("src_id") or "").strip()
            dst = str(e.get("dst_id") or "").strip()
            if src not in positions or dst not in positions:
                continue
            sx, sy = positions[src]
            dx, dy = positions[dst]
            et = str(e.get("type") or "").strip().lower()
            color = "#7F8C8D" if et == "relation" else "#4A90D9"
            ax.annotate(
                "",
                xy=(dx, dy),
                xytext=(sx, sy),
                arrowprops={
                    "arrowstyle": "->",
                    "color": color,
                    "lw": 1.2,
                    "alpha": 0.85,
                    "shrinkA": 10,
                    "shrinkB": 10,
                },
                zorder=1,
            )

        stage_colors = {"localize": "#AED6F1", "deep_dive": "#F9E79F", "expand": "#F5B7B1"}
        for nid, (x, y) in positions.items():
            n = by_id.get(nid) or {}
            stage = str(n.get("stage") or "").strip().lower()
            comp = str(n.get("component") or "").strip() or nid
            t = str(n.get("time") or "").strip()
            conf = float(n.get("deep_dive_confidence", 0.0) or 0.0)
            fill = stage_colors.get(stage, "#D5DBDB")
            circle = plt.Circle((x, y), 0.18, facecolor=fill, edgecolor="#2C3E50", linewidth=1.2, zorder=2)
            ax.add_patch(circle)
            t_short = t.split()[1] if " " in t else t
            label = f"{comp[:20]}\n{t_short}\n{int(round(conf * 100))}%"
            ax.text(x, y - 0.28, label, ha="center", va="top", fontsize=7, zorder=3)

        xs = [p[0] for p in positions.values()]
        ys = [p[1] for p in positions.values()]
        ax.set_xlim(min(xs) - 0.8, max(xs) + 0.8)
        ax.set_ylim(min(ys) - 1.0, max(ys) + 0.7)
        ax.set_title(title, fontsize=11)
        ax.set_axis_off()
        fig.tight_layout()
        try:
            fig.savefig(out_path, dpi=160, bbox_inches="tight")
        finally:
            plt.close(fig)

    def _save_seed_graph_snapshot(
        self,
        *,
        iteration: int,
        seed_ids: list[str],
    ) -> None:
        """Persist seed-only graph snapshot JSON/PNG for this iteration."""
        try:
            out_dir = self.save_dir / "seed_graph"
            out_dir.mkdir(parents=True, exist_ok=True)
            payload = self._build_seed_graph_snapshot(seed_ids)
            payload["metadata"] = {
                "iteration": int(iteration),
                "seed_count": len(seed_ids),
                "seed_ids": [str(x) for x in seed_ids[:200]],
            }
            json_path = out_dir / f"seed_graph_iter_{int(iteration)}.json"
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            png_path = out_dir / f"seed_graph_iter_{int(iteration)}.png"
            self._render_snapshot_png(
                payload,
                png_path,
                title=f"Seed Graph (Iteration {int(iteration)})",
            )
            self._log_service(
                f"[Seed Graph] Saved snapshot: {json_path} | {png_path} "
                f"(nodes={int((payload.get('stats') or {}).get('node_count', 0))}, "
                f"edges={int((payload.get('stats') or {}).get('edge_count', 0))})"
            )
        except Exception as e:
            self._log_service(f"[Seed Graph] Failed to save snapshot: {e}")

    def _run_expand_need_verifier(self, *, iteration: int, seed_ids: list[str]) -> dict:
        """LLM verifier: decide if trace-expand is needed and rank strong local nodes."""
        seed_items: list[dict] = []
        for nid in (seed_ids or []):
            node = self.tree.nodes.get(nid)
            if node is None:
                continue
            comp = str(getattr(node, "component", "") or "").strip()
            if not comp:
                continue
            seed_items.append(
                {
                    "node_id": nid,
                    "component": comp,
                    "level": self._get_level(comp),
                    "time": str(getattr(node, "time", "") or "").strip(),
                    "severity": float(getattr(node, "severity", 0.0) or 0.0),
                    "reason_hint": str(getattr(node, "reason", "") or "").strip(),
                    "kpi": str(getattr(node, "kpi", "") or "").strip(),
                    "deep_dive_verdict": str(getattr(node, "deep_dive_verdict", "") or "").strip().lower(),
                    "deep_dive_reason": str(getattr(node, "deep_dive_reason_class", "") or "").strip(),
                    "deep_dive_confidence": float(
                        getattr(node, "deep_dive_confidence_avg", 0.0)
                        or getattr(node, "deep_dive_confidence", 0.0)
                        or 0.0
                    ),
                }
            )

        verifier_system = """You are an RCA verifier.
Given localized/deep-dive anomalies, decide whether expand should inspect trace/mesh edges.

Output ONLY JSON:
{
  "mode": "local_strong|network_strong|mixed|insufficient",
  "causality_confident": true|false,
  "recommend_trace_expand": true|false,
  "strong_local_components": [{"component":"...","component_level":"node|pod|service","score":0.0-1.0,"why":"..."}]
}

Rules:
- If local resource signals (CPU/memory/disk/io) dominate and are coherent, set mode=local_strong and recommend_trace_expand=false.
- If network/latency/packet/edge-direction signals dominate:
  - If causality is already confident, set recommend_trace_expand=false.
  - If causality is unclear, set recommend_trace_expand=true.
- strong_local_components must include component + component_level, sorted by score desc.
"""
        verifier_user = (
            f"Dataset={self.profile.name!r}, iteration={int(iteration)}\n"
            f"Seed anomalies JSON:\n{json.dumps(seed_items, ensure_ascii=False, indent=2)}\n\n"
            "Return JSON only."
        )

        raw = ""
        payload: dict = {
            "mode": "insufficient",
            "recommend_trace_expand": True,
            "strong_local_nodes": [],
            "error": "",
        }
        try:
            raw = get_chat_completion(
                [
                    {"role": "system", "content": verifier_system},
                    {"role": "user", "content": verifier_user},
                ],
                self.configs,
                temperature=0.0,
            )
            parsed = json.loads(self._extract_json(raw))
            mode = str(parsed.get("mode") or "").strip().lower()
            if mode not in {"local_strong", "network_strong", "mixed", "insufficient"}:
                mode = "insufficient"
            causality_confident = bool(parsed.get("causality_confident", False))
            recommend_trace = bool(parsed.get("recommend_trace_expand", True))
            # Final guardrail: if network is strong but causality is already confident,
            # skip trace-expand to avoid unnecessary expansion.
            if mode == "network_strong" and causality_confident:
                recommend_trace = False
            strong_components = []
            for item in (parsed.get("strong_local_components") or [])[:64]:
                if not isinstance(item, dict):
                    continue
                comp = str(item.get("component") or "").strip()
                level = str(item.get("component_level") or "").strip().lower()
                if not comp:
                    continue
                inferred_level = self._get_level(comp)
                if level not in {"node", "pod", "service"}:
                    level = inferred_level or "service"
                if inferred_level in {"node", "pod", "service"} and level != inferred_level:
                    level = inferred_level
                try:
                    score = float(item.get("score", 0.0) or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                strong_components.append(
                    {
                        "component": comp,
                        "component_level": level,
                        "score": max(0.0, min(1.0, score)),
                        "why": str(item.get("why") or "").strip(),
                    }
                )
            strong_components.sort(
                key=lambda x: (
                    -float(x.get("score", 0.0) or 0.0),
                    str(x.get("component_level") or ""),
                    str(x.get("component") or ""),
                )
            )
            payload = {
                "mode": mode,
                "causality_confident": causality_confident,
                "recommend_trace_expand": recommend_trace,
                "strong_local_components": strong_components[:32],
            }
        except Exception as e:
            payload = {
                "mode": "insufficient",
                "causality_confident": False,
                "recommend_trace_expand": True,
                "strong_local_components": [],
                "error": str(e),
                "raw": raw[:2000],
            }

        out = {
            "iteration": int(iteration),
            "seed_ids": [str(x) for x in (seed_ids or [])[:200]],
            "llm_verdict": payload,
        }
        try:
            out_dir = self.save_dir / "verifier"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"verifier_iter_{int(iteration)}.json"
            out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log_service(
                f"[Verifier] llm mode={payload.get('mode')!r}, "
                f"recommend_trace_expand={bool(payload.get('recommend_trace_expand', True))}, "
                f"strong_local_components={[x.get('component') for x in (payload.get('strong_local_components') or [])[:8]]} "
                f"(saved: {out_path})"
            )
        except Exception as e:
            self._log_service(f"[Verifier] Failed to save verifier snapshot: {e}")
        return payload

    def _save_causal_graph_snapshot(
        self,
        *,
        iteration: int,
        seed_ids: list[str],
        new_child_ids: list[str],
    ) -> None:
        """Persist causal-graph snapshot JSON for this iteration."""
        try:
            out_dir = self.save_dir / "causal_graph"
            out_dir.mkdir(parents=True, exist_ok=True)
            payload = self._build_causal_graph_snapshot()
            payload["metadata"] = {
                "iteration": int(iteration),
                "seed_count": len(seed_ids),
                "seed_ids": [str(x) for x in seed_ids[:200]],
                "new_child_count": len(new_child_ids),
                "new_child_ids": [str(x) for x in new_child_ids[:500]],
            }
            json_path = out_dir / f"causal_graph_iter_{int(iteration)}.json"
            with json_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            png_path = out_dir / f"causal_graph_iter_{int(iteration)}.png"
            self._render_snapshot_png(
                payload,
                png_path,
                title=f"Causal Graph (Iteration {int(iteration)})",
            )
            self._log_service(
                f"[Causal Graph] Saved snapshot: {json_path} | {png_path} "
                f"(nodes={int((payload.get('stats') or {}).get('node_count', 0))}, "
                f"edges={int((payload.get('stats') or {}).get('edge_count', 0))})"
            )
        except Exception as e:
            self._log_service(f"[Causal Graph] Failed to save snapshot: {e}")

    def _run_iterative_deep_dive_expand(self, seed_ids: list[str], max_iterations: int) -> None:
        frontier = list(seed_ids)
        expanded_from: set[str] = set()
        for iteration in range(1, max_iterations + 1):
            if not frontier:
                break

            self._log_agent(f"=== Iteration {iteration}: Deep Dive ===", step_boundary=True)
            logger.info("Iteration %d: deep dive on frontier=%s", iteration, frontier)
            survivors: list[str] = []
            for node_id in frontier:
                if node_id not in self.tree.nodes:
                    continue
                if self.enable_deep_dive and self.use_controller_deep_dive and self.problem:
                    survivors.extend(self._stage2_controller_deep_dive([node_id]))
                else:
                    survivors.append(node_id)
            self.tree.save(self.save_dir / "tree.json")
            self._update_live_view()

            if not self.enable_expand:
                break

            # Build global expand seeds from all deep-dive survivors accumulated so far,
            # not only from the current frontier.
            expand_seeds: list[str] = []
            for nid, node in self.tree.nodes.items():
                if nid in {"root"} or nid in expanded_from:
                    continue
                if node is None or str(getattr(node, "status", "") or "") == "pruned":
                    continue
                if str(getattr(node, "stage", "") or "").strip().lower() != "localize":
                    continue
                level = self._get_level(getattr(node, "component", "") or "")
                if level not in {"pod", "service"}:
                    continue
                verdict = str(getattr(node, "deep_dive_verdict", "") or "").strip().lower()
                if verdict != "anomaly":
                    continue
                conf_avg = float(
                    getattr(node, "deep_dive_confidence_avg", 0.0)
                    or getattr(node, "deep_dive_confidence", 0.0)
                    or 0.0
                )
                if conf_avg < 0.50:
                    continue
                expand_seeds.append(nid)

            # Stable ordering: stronger confidence first, then recent time, then id.
            def _seed_sort_key(nid: str):
                n = self.tree.nodes.get(nid)
                if n is None:
                    return (0.0, "", nid)
                c = float(
                    getattr(n, "deep_dive_confidence_avg", 0.0)
                    or getattr(n, "deep_dive_confidence", 0.0)
                    or 0.0
                )
                t = str(getattr(n, "time", "") or "")
                return (-c, t, nid)

            expand_seeds = sorted(list(dict.fromkeys(expand_seeds)), key=_seed_sort_key)
            for nid in expand_seeds:
                expanded_from.add(nid)
            self._save_seed_graph_snapshot(
                iteration=iteration,
                seed_ids=expand_seeds,
            )

            if not expand_seeds:
                frontier = []
                break

            self._log_agent(f"=== Iteration {iteration}: Expand ===", step_boundary=True)
            logger.info("Iteration %d: expand from seeds=%s", iteration, expand_seeds)
            next_frontier: list[str] = []
            # Seed-scoped expand: run expand for each seed independently.
            for seed_id in expand_seeds:
                seed_node = self.tree.nodes.get(seed_id)
                logger.info(
                    "Iteration %d: expand for seed=%s(%s)",
                    iteration,
                    seed_id,
                    getattr(seed_node, "component", ""),
                )
                next_frontier.extend(self._stage3_controller_expand([seed_id], []))
            self._save_causal_graph_snapshot(
                iteration=iteration,
                seed_ids=expand_seeds,
                new_child_ids=list(dict.fromkeys(next_frontier)),
            )
            verifier_outcome = self._run_expand_need_verifier(
                iteration=iteration,
                seed_ids=expand_seeds,
            )
            if not bool(verifier_outcome.get("recommend_trace_expand", True)):
                self._log_service(
                    f"[Iterative] Stop after expand at iteration {iteration}: verifier mode="
                    f"{verifier_outcome.get('mode')!r} (local signals stronger)."
                )
                frontier = []
                break
            if not next_frontier:
                self._log_service(
                    f"[Iterative] Stop at iteration {iteration}: expand produced 0 new candidates."
                )
                frontier = []
                break
            frontier = list(dict.fromkeys(next_frontier))
            self.tree.save(self.save_dir / "tree.json")
            self._update_live_view()

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
        if candidate_ids:
            kept_lines: list[str] = []
            for cid in candidate_ids:
                n = self.tree.nodes.get(cid)
                if not n:
                    continue
                kept_lines.append(
                    f"- {cid}: component={n.component!r}, time={n.time!r}, "
                    f"severity={float(getattr(n, 'severity', 0.0) or 0.0):.1f}, reason={n.reason!r}"
                )
            if kept_lines:
                self._log_service(
                    "[Localize] Final kept nodes after stage1 pruning:\n" + "\n".join(kept_lines)
                )
        self._update_live_view()

        # Temporarily disabled: trace image-based localization/precompute path.
        # self._precompute_trace_edges(candidate_ids)
        # self._save_trace_anomaly_edges()
        self._log_service("[Trace] Trace image localization/precompute is temporarily disabled.")

        # Sort candidates by severity (highest first) so we traverse worst anomalies first
        candidate_ids = self._sort_candidates_by_priority(candidate_ids)
        # Existing-count map for expand stop rule:
        # localized components start at 1, then increment whenever expand inspects/proposes them.
        self._expand_component_existing_count = {}
        for cid in candidate_ids:
            node = self.tree.nodes.get(cid)
            comp = (getattr(node, "component", "") or "").strip() if node else ""
            if comp:
                self._expand_component_existing_count[comp] = 1

        # ── Save tree (so far), build host containment groups, render ──
        tree_path = self.save_dir / "tree.json"
        self.tree.save(tree_path)
        if candidate_ids:
            self._build_localization_host_containment_groups(candidate_ids)
            self._render_localization_dependency_graph(candidate_ids)
        if self.render_localization_timeline and candidate_ids:
            self._render_localization_timeline(tree_path)

        # ── Stage 2/3: iterative deep dive → expand loop ─────────────
        if candidate_ids:
            logger.info("Stage 2/3: Iterative Deep Dive → Expand")
            self._log_agent(
                "=== Stage 2/3: Iterative Deep Dive → Expand ===",
                step_boundary=True,
            )
            self._run_iterative_deep_dive_expand(candidate_ids, max_iterations=max_iterations)
            self.tree.save(tree_path)
            self._update_live_view()
        else:
            logger.info("Stage 2/3 skipped: no localization candidates")
            self._log_agent(
                "=== Stage 2/3 skipped: no localization candidates ===",
                step_boundary=True,
            )

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
            if not self.enable_trace_path_vision_localize:
                self._log_agent("[Localize] Trace anomalous path vision critic disabled by config")
            elif hasattr(self.actions, "get_trace_anomalous_path_graph"):
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

        # Keep only top severity localization candidates up to 1/3 of total possible components.
        # This narrows stage1 fan-out while preserving the strongest hypotheses.
        uniq_candidate_ids: list[str] = []
        seen_ids: set[str] = set()
        for cid in candidate_ids:
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                uniq_candidate_ids.append(cid)
        candidate_ids = uniq_candidate_ids

        all_components = {
            str(c).strip()
            for c in (self.profile.possible_components or [])
            if str(c).strip()
        }
        if not all_components:
            all_components = {
                str(c).strip()
                for comps in (self.profile.component_levels or {}).values()
                for c in (comps or [])
                if str(c).strip()
            }
        total_component_candidates = len(all_components)
        if total_component_candidates > 0 and candidate_ids:
            keep_limit = max(1, (total_component_candidates + 2) // 3)  # ceil(n/3)
            if len(candidate_ids) > keep_limit:
                sorted_ids = self._sort_candidates_by_priority(candidate_ids)
                keep_ids = set(sorted_ids[:keep_limit])
                dropped = [cid for cid in candidate_ids if cid not in keep_ids]
                for cid in dropped:
                    node = self.tree.nodes.get(cid)
                    if not node:
                        continue
                    node.status = "pruned"
                    prev = (node.evidence or "").strip()
                    note = "stage1_top_fraction_prune(keep_top_1_3_by_severity)"
                    node.evidence = f"{prev} || {note}" if prev else note
                candidate_ids = [cid for cid in sorted_ids if cid in keep_ids]
                self._log_service(
                    f"[Localize] Top-1/3 severity pruning applied: kept={len(candidate_ids)} "
                    f"dropped={len(dropped)} total_components={total_component_candidates} "
                    f"limit={keep_limit}"
                )

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

    def _normalize_expand_component_hint(self, value: str | None) -> str:
        comp = str(value or "").strip()
        if not comp:
            return ""
        dataset = (self.profile.name or "").replace("openrca_", "")
        if dataset.startswith("market") and comp.startswith("os_node-"):
            comp = comp.replace("os_", "", 1)
        return comp

    def _resolve_expand_component_hint(
        self,
        raw_hint: str | None,
        known_components: set[str],
    ) -> str | None:
        hint = self._normalize_expand_component_hint(raw_hint)
        if not hint:
            return None
        if hint in known_components:
            return hint

        # Accept exact alias matches when the hint is a pod suffix like shippingservice-1.
        for comp in known_components:
            aliases = set(self._component_dependency_aliases(comp))
            if hint in aliases:
                return comp

        text = f" {hint} "
        for comp in sorted(known_components, key=len, reverse=True):
            if f" {comp} " in text or comp == hint:
                return comp
        return None

    def _build_deep_dive_target_relation_map(
        self,
        node: TreeNode,
        known_components: set[str],
    ) -> dict[str, list[dict]]:
        rel_map: dict[str, list[dict]] = {}
        for target in getattr(node, "deep_dive_edge_targets", []) or []:
            if not isinstance(target, dict):
                continue
            resolved = self._resolve_expand_component_hint(
                target.get("component"),
                known_components,
            )
            if not resolved or resolved == node.component:
                continue
            rel_map.setdefault(resolved, []).append(
                {
                    "relation_family": "deep_dive_target",
                    "relation_type": str(target.get("relation_hint") or "requested_edge_check").strip() or "requested_edge_check",
                    "anomaly_type": None,
                    "time": node.time,
                    "label": "deep_dive_target",
                    "metadata": {
                        "why": str(target.get("why") or "").strip(),
                        "requested_component": str(target.get("component") or "").strip(),
                        "from_component": node.component,
                    },
                }
            )
        return rel_map

    def _build_trace_expand_target_relation_map(
        self,
        node: TreeNode,
        edge_targets: list[dict],
        known_components: set[str],
    ) -> dict[str, list[dict]]:
        """Convert iterative trace-expand edge targets into relation metadata map."""
        rel_map: dict[str, list[dict]] = {}
        for target in edge_targets or []:
            if not isinstance(target, dict):
                continue
            resolved = self._resolve_expand_component_hint(
                target.get("component"),
                known_components,
            )
            if not resolved or resolved == node.component:
                continue
            relation_hint = str(target.get("relation_hint") or "trace_expand").strip() or "trace_expand"
            relation_type = relation_hint.replace(" ", "_")
            try:
                conf = float(target.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            rel_map.setdefault(resolved, []).append(
                {
                    "relation_family": "trace_expand_target",
                    "relation_type": relation_type,
                    "anomaly_type": str(target.get("anomaly_type") or "").strip() or None,
                    "time": str(target.get("time") or node.time or "").strip() or None,
                    "label": "trace_expand_target",
                    "metadata": {
                        "why": str(target.get("why") or "").strip(),
                        "requested_component": str(target.get("component") or "").strip(),
                        "from_component": node.component,
                        "confidence": conf,
                        "evidence_source": str(target.get("evidence_source") or "").strip(),
                        "causal_direction": str(target.get("causal_direction") or "").strip().lower(),
                        "component_level": str(target.get("component_level") or "").strip().lower(),
                        "why_not_reverse": str(target.get("why_not_reverse") or "").strip(),
                    },
                }
            )
        return rel_map

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

    def _component_host_from_deployment(self, component: str) -> str | None:
        comp = str(component or "").strip()
        if not comp:
            return None
        try:
            graphs = get_graphs(self._get_dependency_graph_dataset_key())
            deploy_g = graphs.get("deployment_graph") or {}
        except Exception:
            return None
        aliases = self._component_dependency_aliases(comp)
        for alias in aliases:
            if alias in deploy_g:
                return alias
        for host, members in deploy_g.items():
            member_set = set(members or [])
            for alias in aliases:
                if alias in member_set:
                    return str(host or "").strip() or None
        return None

    def _has_broad_colocated_pod_impact(
        self,
        node_component: str,
        node_time: str | None,
        *,
        window_min: int = 10,
        min_ratio: float = 0.5,
    ) -> tuple[str, list[str], int, float]:
        host = self._component_host_from_deployment(node_component)
        if not host:
            return "unknown", [], 0, 0.0
        try:
            graphs = get_graphs(self._get_dependency_graph_dataset_key())
            deploy_g = graphs.get("deployment_graph") or {}
        except Exception:
            return "unknown", [], 0, 0.0

        hosted_members = list(deploy_g.get(host) or [])
        hosted_pods = [str(m).strip() for m in hosted_members if self._get_level(str(m).strip()) == "pod"]
        hosted_pods = sorted({p for p in hosted_pods if p})
        hosted_count = len(hosted_pods)
        if hosted_count == 0:
            return "unknown", [], 0, 0.0

        alias_to_hosted: dict[str, str] = {}
        for pod in hosted_pods:
            for alias in self._component_dependency_aliases(pod):
                alias_to_hosted.setdefault(alias, pod)

        impacted_hosted: set[str] = set()
        for n in self.tree.nodes.values():
            if n is None or n.id == "root":
                continue
            if str(getattr(n, "status", "") or "") == "pruned":
                continue
            comp = str(getattr(n, "component", "") or "").strip()
            if not comp or self._get_level(comp) != "pod":
                continue
            pod_host = self._component_host_from_deployment(comp)
            if pod_host != host:
                continue
            verdict = str(getattr(n, "deep_dive_verdict", "") or "").strip().lower()
            localized = bool(getattr(n, "localized_match", False))
            if verdict != "anomaly" and not localized:
                continue
            if node_time and str(getattr(n, "time", "") or "").strip():
                if not self._is_time_within_minutes(node_time, n.time, window_min=window_min):
                    continue
            matched_hosted = None
            for alias in self._component_dependency_aliases(comp):
                if alias in alias_to_hosted:
                    matched_hosted = alias_to_hosted[alias]
                    break
            if matched_hosted:
                impacted_hosted.add(matched_hosted)

        impacted = sorted(impacted_hosted)
        ratio = len(impacted) / float(hosted_count)
        if ratio >= float(min_ratio):
            return "supported", impacted, hosted_count, ratio
        return "weak", impacted, hosted_count, ratio

    def _pick_best_colocated_pod_candidate(
        self,
        node_component: str,
        node_time: str | None,
        *,
        window_min: int = 10,
    ) -> TreeNode | None:
        host = self._component_host_from_deployment(node_component)
        if not host:
            return None
        candidates: list[TreeNode] = []
        for n in self.tree.nodes.values():
            if n is None or n.id == "root":
                continue
            if str(getattr(n, "status", "") or "") == "pruned":
                continue
            comp = str(getattr(n, "component", "") or "").strip()
            if not comp or self._get_level(comp) != "pod":
                continue
            if self._component_host_from_deployment(comp) != host:
                continue
            if node_time and str(getattr(n, "time", "") or "").strip():
                if not self._is_time_within_minutes(node_time, n.time, window_min=window_min):
                    continue
            verdict = str(getattr(n, "deep_dive_verdict", "") or "").strip().lower()
            if verdict != "anomaly":
                continue
            candidates.append(n)
        if not candidates:
            return None
        candidates.sort(
            key=lambda x: (
                float(getattr(x, "deep_dive_confidence_avg", 0.0) or 0.0),
                float(getattr(x, "deep_dive_confidence", 0.0) or 0.0),
                float(getattr(x, "confidence", 0.0) or 0.0),
                float(getattr(x, "severity", 0.0) or 0.0),
            ),
            reverse=True,
        )
        return candidates[0]

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
        dataset = (self.profile.name or "").replace("openrca_", "")
        if dataset.startswith("market") and comp.startswith("os_node-"):
            comp = comp.replace("os_", "", 1)
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
        """Iterative controller-driven deep dive delegated to deep_dive_stage.py."""
        if not candidate_ids or not self.problem:
            return []
        cid = candidate_ids[0]
        node = self.tree.nodes[cid]
        level = self._get_level(node.component)
        outcome = run_deep_dive_controller(
            problem=self.problem,
            actions=self.actions,
            profile=self.profile,
            namespace=self.namespace,
            llm_configs=self.configs,
            sprint=self.sprint,
            node=node,
            level=level,
            time_range=self.time_range,
            normalize_time=self._normalize_outlier_time,
        )
        if outcome.verdict == "anomaly":
            final_reason = outcome.reason or node.reason
            reason_class = (outcome.reason or self._normalize_reason_to_class(node.reason)) or None
        else:
            final_reason = None
            reason_class = None
        self._upsert_deep_dive_on_node(
            node,
            reason=final_reason,
            reason_class=reason_class,
            verdict=outcome.verdict,
            confidence=outcome.confidence,
            explanation=outcome.explanation,
            checked_reasons=outcome.checked_reasons,
            next_kpis=outcome.next_kpis,
            edge_targets=outcome.edge_targets,
            time_str=outcome.time or node.time,
        )
        dd_avg = float(getattr(node, "deep_dive_confidence_avg", 0.0) or 0.0)
        dd_count = int(getattr(node, "deep_dive_confidence_count", 0) or 0)
        self._log_service(
            f"[Deep Dive] component={node.component!r} verdict={outcome.verdict!r} "
            f"reason={outcome.reason!r} confidence={outcome.confidence:.2f} "
            f"avg={dd_avg:.2f} count={dd_count} "
            f"next_kpis={outcome.next_kpis[:8]!r} edge_targets={outcome.edge_targets[:4]!r}"
        )
        if outcome.verdict == "noise":
            return []
        if outcome.confidence < 0.30:
            return []
        return [cid]

    def _resolve_namespace_base_path(self) -> Path | None:
        base = getattr(getattr(self.actions, "static_app", None), "base_path", None)
        ns = str(self.namespace or "").strip()
        if base is None or not ns:
            return None
        ns_path = Path(base) / ns
        if ns_path.exists():
            return ns_path
        return None

    def _format_edge_tool_table_raw(self, table, *, label: str) -> str:
        try:
            import pandas as pd
        except Exception:
            pd = None
        if pd is None or not isinstance(table, pd.DataFrame):
            return f"[{label}]\nunavailable (non-DataFrame result)"
        if table.empty:
            return f"[{label}]\n(empty DataFrame)"
        return f"[{label}]\n{table.to_string()}"

    def _build_seed_trace_mesh_dependency_summary(
        self,
        node: TreeNode,
        *,
        window_min: int = 5,
        top_k: int = 10,
    ) -> str:
        dataset_name = str(self.profile.name or "").lower()
        if not dataset_name.startswith("openrca_market"):
            return "(precompute unavailable for this dataset)"
        base_path = self._resolve_namespace_base_path()
        if base_path is None:
            return "(precompute unavailable: namespace telemetry path not found)"

        seed_time = str(getattr(node, "time", "") or "").strip()
        if not seed_time:
            return f"(precompute skipped: missing seed time for component={node.component!r})"
        try:
            dt = datetime.strptime(seed_time, "%Y-%m-%d %H:%M:%S")
            start_time = (dt - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
            end_time = (dt + timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return f"(precompute skipped: invalid seed time format={seed_time!r})"

        from clients.tree_traversal.tools.trace_expand.market_cb1 import (
            get_edge_error_rate_minutely,
            get_edge_latency_minutely,
        )

        comp = str(node.component or "").strip()
        # For node seeds, use pod granularity so mesh edges can align with colocated pod aliases.
        level = self._get_level(comp)
        if level in {"node", "pod"}:
            granularity = "pod"
        else:
            granularity = "service"

        sections: list[str] = [
            f"Seed={comp!r}, seed_time={seed_time!r}, window=[{start_time} , {end_time}] (UTC), granularity={granularity}"
        ]
        calls = [
            (
                "latency/caller (trace+mesh)",
                get_edge_latency_minutely,
                {
                    "base_path": str(base_path),
                    "focus_component": comp,
                    "direction": "caller",
                    "start_time": start_time,
                    "end_time": end_time,
                    "source": "both",
                    "granularity": granularity,
                    "agg": ["p50", "p95"],
                    "top_k": int(top_k),
                },
            ),
            (
                "latency/callee (trace+mesh)",
                get_edge_latency_minutely,
                {
                    "base_path": str(base_path),
                    "focus_component": comp,
                    "direction": "callee",
                    "start_time": start_time,
                    "end_time": end_time,
                    "source": "both",
                    "granularity": granularity,
                    "agg": ["p50", "p95"],
                    "top_k": int(top_k),
                },
            ),
            (
                "error_rate/caller (trace+mesh)",
                get_edge_error_rate_minutely,
                {
                    "base_path": str(base_path),
                    "focus_component": comp,
                    "direction": "caller",
                    "start_time": start_time,
                    "end_time": end_time,
                    "source": "both",
                    "granularity": granularity,
                    "agg": ["mean", "max"],
                    "top_k": int(top_k),
                },
            ),
            (
                "error_rate/callee (trace+mesh)",
                get_edge_error_rate_minutely,
                {
                    "base_path": str(base_path),
                    "focus_component": comp,
                    "direction": "callee",
                    "start_time": start_time,
                    "end_time": end_time,
                    "source": "both",
                    "granularity": granularity,
                    "agg": ["mean", "max"],
                    "top_k": int(top_k),
                },
            ),
        ]

        for label, fn, kwargs in calls:
            try:
                table = fn(**kwargs)
                sections.append(self._format_edge_tool_table_raw(table, label=label))
            except Exception as e:
                sections.append(f"[{label}]\nfailed ({e})")
        return "\n".join(sections)

    def _stage3_controller_expand(
        self, hypothesis_ids: list[str], candidate_ids: list[str],
    ) -> list[str]:
        """Global trace-expand over multiple hypotheses: add multiple next components."""
        self._last_expand_stop_due_existing_count = False
        self._last_expand_stop_components = []
        if not hypothesis_ids or not self.problem:
            return []
        hypothesis_nodes: list[TreeNode] = [
            self.tree.nodes[hid] for hid in hypothesis_ids if hid in self.tree.nodes
        ]
        if not hypothesis_nodes:
            return []

        anchor = hypothesis_nodes[0]
        anchor_level = self._get_level(getattr(anchor, "component", "") or "")
        if anchor_level not in {"pod", "service"}:
            self._log_service(
                f"[Expand] Skip trace-expand for non pod/service seed: component={anchor.component!r}, level={anchor_level!r}"
            )
            return []
        hypothesis_by_component: dict[str, tuple[str, TreeNode]] = {
            str(n.component or "").strip(): (hid, n)
            for hid, n in zip(hypothesis_ids, hypothesis_nodes)
            if str(n.component or "").strip()
        }

        merged_deploy_map: dict[str, list[dict]] = {}
        merged_topology_map: dict[str, list[dict]] = {}
        known_components = set(self.profile.possible_components or [])

        for node in hypothesis_nodes:
            _dr, deploy_map, _dg = self._get_deployment_relation_candidates(node.component)
            _tr, topology_map = self._get_topology_relation_candidates(node.component)
            merged_deploy_map = self._merge_relation_candidate_maps(
                merged_deploy_map,
                deploy_map,
            )
            merged_topology_map = self._merge_relation_candidate_maps(
                merged_topology_map,
                topology_map,
            )
            known_components |= set(deploy_map.keys()) | set(topology_map.keys())

        merged_deep_dive_map: dict[str, list[dict]] = {}
        for node in hypothesis_nodes:
            deep_map = self._build_deep_dive_target_relation_map(node, known_components)
            merged_deep_dive_map = self._merge_relation_candidate_maps(
                merged_deep_dive_map,
                deep_map,
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

        trace_expand_relation_map: dict[str, list[dict]] = {}
        trace_expand_edge_targets: list[dict] = []
        seed_relation_map = self._merge_relation_candidate_maps(
            merged_deploy_map,
            merged_topology_map,
            merged_deep_dive_map,
        )
        seed_candidates = sorted(seed_relation_map.keys())
        existing_children_under_parent = {
            (
                (n.parent_id or "").strip(),
                (n.component or "").strip(),
            )
            for n in self.tree.nodes.values()
            if (n.component or "").strip()
        }
        hypothesis_parent_ids = [
            hid for hid in hypothesis_ids if hid in self.tree.nodes
        ]
        if not seed_candidates:
            self._log_service(
                f"[Expand] hypotheses={[n.component for n in hypothesis_nodes]!r} produced 0 relation candidates for trace-expand."
            )
            return []

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
        tree_summary = self._summarize_tree_for_expand(hypothesis_parent_ids[0])
        hypothesis_summary = (
            f"anchor_component={anchor.component!r}, anchor_time={anchor.time!r}, anchor_reason={anchor.reason!r}, "
            f"anchor_deep_dive_verdict={getattr(anchor, 'deep_dive_verdict', None)!r}, "
            f"anchor_deep_dive_reason={getattr(anchor, 'deep_dive_reason_class', None)!r}, "
            f"query_window={query_window_str or '(unknown)'}"
        )
        seed_time_str = str(getattr(anchor, "time", "") or "").strip()
        seed_window_text = ""
        if seed_time_str:
            try:
                dt = datetime.strptime(seed_time_str, "%Y-%m-%d %H:%M:%S")
                ws = (dt - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
                we = (dt + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
                seed_window_text = f"[{ws} , {we}]"
            except Exception:
                seed_window_text = ""
        precomputed_dependency_summary = self._build_seed_trace_mesh_dependency_summary(
            anchor,
            window_min=5,
            top_k=10,
        )
        full_lines = [
            str(x).rstrip()
            for x in str(precomputed_dependency_summary or "").splitlines()
            if str(x).strip()
        ]
        if full_lines:
            self._log_service(
                "[Trace Expand] Precomputed dependency summary (full):\n"
                + "\n".join(full_lines)
            )
        target_hypotheses = []
        for hid in hypothesis_parent_ids:
            n = self.tree.nodes.get(hid)
            if n is None:
                continue
            target_hypotheses.append(
                {
                    "node_id": hid,
                    "component": n.component,
                    "time": n.time,
                    "level": self._get_level(n.component),
                    "localized_reason": n.reason,
                    "deep_dive_verdict": getattr(n, "deep_dive_verdict", None),
                    "deep_dive_reason": getattr(n, "deep_dive_reason_class", None),
                    "deep_dive_confidence": float(
                        getattr(n, "deep_dive_confidence_avg", 0.0)
                        or getattr(n, "deep_dive_confidence", 0.0)
                        or 0.0
                    ),
                    "deep_dive_next_kpis": list(getattr(n, "deep_dive_next_kpis", []) or [])[:12],
                }
            )
        seed_lines: list[str] = []
        for comp in seed_candidates[:80]:
            rel_infos = seed_relation_map.get(comp) or []
            families = sorted(
                {
                    str(info.get("relation_family") or "").strip()
                    for info in rel_infos
                    if str(info.get("relation_family") or "").strip()
                }
            )
            rel_types = sorted(
                {
                    str(info.get("relation_type") or "").strip()
                    for info in rel_infos
                    if str(info.get("relation_type") or "").strip()
                }
            )
            seed_lines.append(
                f"- {comp}: families={families or ['unknown']}, relation_types={rel_types or ['unknown']}"
            )
        try:
            trace_expand_outcome = run_trace_expand_controller(
                problem=locked_problem,
                llm_configs=self.configs,
                sprint=self.sprint,
                actions=self.actions,
                profile=self.profile,
                namespace=self.namespace,
                dataset_name=self.profile.name or "",
                hypothesis_summary=hypothesis_summary,
                target_hypotheses=target_hypotheses,
                tree_summary=tree_summary,
                system_structure_summary="\n".join(seed_lines),
                candidate_seed_summary=", ".join(seed_candidates[:80]),
                precomputed_dependency_summary=precomputed_dependency_summary,
                initial_user_message=(
                    f"Target hypotheses={ [x.get('component') for x in target_hypotheses] !r}. "
                    f"Seed-centered query window={seed_window_text or '(unknown; derive from seed time)'} (about ±5 minutes). "
                    "Use the precomputed trace/mesh caller-callee dependency snapshot first, "
                    "then iterate with tools if needed, and submit related anomalous child components for deep-dive as edge_targets."
                ),
                parent_components=[
                    str(self.tree.nodes[hid].component or "").strip()
                    for hid in hypothesis_parent_ids
                    if hid in self.tree.nodes and str(self.tree.nodes[hid].component or "").strip()
                ],
            )
            trace_expand_edge_targets = [
                x for x in (trace_expand_outcome.edge_targets or []) if isinstance(x, dict)
            ]
        except Exception as e:
            self._log_service(f"[Trace Expand] failed: {e}")
            return []

        trace_expand_relation_map = self._build_trace_expand_target_relation_map(
            anchor,
            trace_expand_edge_targets,
            known_components,
        )
        if trace_expand_relation_map:
            self._log_service(
                f"[Trace Expand] Added {len(trace_expand_relation_map)} verified edge target(s): "
                + ", ".join(sorted(trace_expand_relation_map.keys())[:20])
            )
        if not trace_expand_edge_targets:
            self._log_service(
                f"[Expand] No trace-expand edge targets for hypotheses="
                f"{[n.component for n in hypothesis_nodes]!r}."
            )
            return []

        normalized_targets: list[dict] = []
        for target in trace_expand_edge_targets:
            resolved = self._resolve_expand_component_hint(target.get("component"), known_components)
            from_hyp = str(target.get("from_hypothesis_component") or "").strip()
            if not resolved:
                continue
            resolved_level = self._get_level(resolved)
            if resolved_level not in {"pod", "service"}:
                continue
            try:
                conf = float(target.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            time_hint = str(target.get("time") or "").strip()
            if not time_hint:
                if from_hyp and from_hyp in hypothesis_by_component:
                    time_hint = str(hypothesis_by_component[from_hyp][1].time or "").strip()
                if not time_hint:
                    time_hint = str(anchor.time or "").strip()
            normalized_targets.append(
                {
                    "component": resolved,
                    "confidence": conf,
                    "time": time_hint,
                    "relation_hint": str(target.get("relation_hint") or "trace_expand").strip() or "trace_expand",
                    "why": str(target.get("why") or "").strip(),
                    "evidence_source": str(target.get("evidence_source") or "").strip(),
                    "from_hypothesis_component": from_hyp,
                    "causal_direction": str(target.get("causal_direction") or "").strip().lower(),
                    "component_level": resolved_level,
                    "why_not_reverse": str(target.get("why_not_reverse") or "").strip(),
                }
            )
        if not normalized_targets:
            self._log_service(
                f"[Expand] Trace-expand targets were empty or unresolved for hypotheses="
                f"{[n.component for n in hypothesis_nodes]!r}."
            )
            return []

        by_component: dict[str, dict] = {}
        for item in normalized_targets:
            comp = str(item.get("component") or "").strip()
            if not comp:
                continue
            prev = by_component.get(comp)
            if prev is None or float(item.get("confidence", 0.0) or 0.0) > float(
                prev.get("confidence", 0.0) or 0.0
            ):
                by_component[comp] = item
        ranked = sorted(
            by_component.values(),
            key=lambda x: (
                -float(x.get("confidence", 0.0) or 0.0),
                str(x.get("component") or ""),
            ),
        )

        selected_items: list[dict] = []
        for item in ranked:
            comp = str(item.get("component") or "").strip()
            if not comp:
                continue
            try:
                comp_conf = float(item.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                comp_conf = 0.0
            if comp_conf < 0.50:
                continue
            from_hyp = str(item.get("from_hypothesis_component") or "").strip()
            parent_id = hypothesis_parent_ids[0]
            if from_hyp and from_hyp in hypothesis_by_component:
                parent_id = hypothesis_by_component[from_hyp][0]
            if (parent_id, comp) in existing_children_under_parent:
                continue
            current_count = int(self._expand_component_existing_count.get(comp, 0) or 0) + 1
            self._expand_component_existing_count[comp] = current_count
            selected_items.append(item)

        if not selected_items:
            self._log_service(
                f"[Expand] Trace-expand selected only low-confidence or duplicate/already-expanded components for hypotheses="
                f"{[n.component for n in hypothesis_nodes]!r}."
            )
            return []

        new_ids: list[str] = []
        for selected in selected_items:
            comp = str(selected.get("component") or "").strip()
            from_hyp = str(selected.get("from_hypothesis_component") or "").strip()
            parent_id = hypothesis_parent_ids[0]
            parent_component = anchor.component
            parent_time = anchor.time
            if from_hyp and from_hyp in hypothesis_by_component:
                parent_id = hypothesis_by_component[from_hyp][0]
                parent_component = hypothesis_by_component[from_hyp][1].component
                parent_time = hypothesis_by_component[from_hyp][1].time
            t_str = str(selected.get("time") or parent_time or "").strip() or parent_time
            try:
                comp_conf = float(selected.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                comp_conf = 0.0
            relation_hint = str(selected.get("relation_hint") or "trace_expand").strip() or "trace_expand"
            why = str(selected.get("why") or "").strip()
            source = str(selected.get("evidence_source") or "").strip()
            causal_direction = str(selected.get("causal_direction") or "").strip().lower()
            component_level = str(selected.get("component_level") or "").strip().lower()
            why_not_reverse = str(selected.get("why_not_reverse") or "").strip()

            rel_infos = trace_expand_relation_map.get(comp) or [
                {
                    "relation_family": "trace_expand_target",
                    "relation_type": relation_hint.replace(" ", "_"),
                    "anomaly_type": None,
                    "time": t_str,
                    "label": "trace_expand_target",
                    "metadata": {
                        "why": why,
                        "requested_component": comp,
                        "from_component": parent_component,
                        "confidence": comp_conf,
                        "evidence_source": source,
                        "causal_direction": causal_direction,
                        "component_level": component_level,
                        "why_not_reverse": why_not_reverse,
                    },
                }
            ]
            rel_str = self._relation_label_from_infos(rel_infos)
            evidence_parts = [
                "trace_expand_multi",
                f"relation_hint={relation_hint}",
                f"confidence={comp_conf:.2f}",
            ]
            if source:
                evidence_parts.append(f"source={source}")
            if causal_direction:
                evidence_parts.append(f"causal_direction={causal_direction}")
            if component_level:
                evidence_parts.append(f"component_level={component_level}")
            if why_not_reverse:
                evidence_parts.append(f"why_not_reverse={why_not_reverse}")
            if why:
                evidence_parts.append(f"why={why}")
            evidence = " | ".join(evidence_parts)

            rc_id = self.tree.add_candidate(
                stage="expand",
                component=comp,
                time_str=t_str,
                parent_id=parent_id,
                evidence=evidence,
                relation=rel_str,
                confidence=comp_conf,
                localized_match=False,
                localized_time=None,
                localized_severity=0.0,
            )
            self._attach_expand_relation_edges(
                parent_id,
                parent_component,
                rc_id,
                comp,
                rel_infos,
            )
            self._log_service(
                f"  + New candidate from trace-expand: {comp} "
                f"(time={t_str!r}, confidence={comp_conf:.2f}, relation={rel_str!r}, evidence={evidence!r})"
            )
            new_ids.append(rc_id)
        self._update_live_view()
        return new_ids

    # ── Stage 2: Deep Dive (fixed pipeline) ───────────────────────────

    def _upsert_deep_dive_on_node(
        self,
        node: TreeNode,
        *,
        reason: str | None,
        reason_class: str | None,
        verdict: str | None = None,
        confidence: float,
        explanation: str,
        checked_reasons: list[dict] | None = None,
        next_kpis: list[str] | None = None,
        edge_targets: list[dict] | None = None,
        time_str: str | None = None,
    ) -> None:
        """Store deep-dive result on the existing node instead of creating a child node."""
        if time_str:
            node.time = time_str
        node.deep_dive_reason = reason or node.deep_dive_reason
        node.deep_dive_reason_class = reason_class or node.deep_dive_reason_class
        node.deep_dive_time = node.time
        conf_val = float(confidence or 0.0)
        node.deep_dive_confidence = max(
            float(getattr(node, "deep_dive_confidence", 0.0) or 0.0),
            conf_val,
        )
        prev_count = int(getattr(node, "deep_dive_confidence_count", 0) or 0)
        prev_avg = float(getattr(node, "deep_dive_confidence_avg", 0.0) or 0.0)
        new_count = prev_count + 1
        new_avg = ((prev_avg * prev_count) + conf_val) / float(new_count)
        node.deep_dive_confidence_count = new_count
        node.deep_dive_confidence_avg = new_avg
        if explanation:
            node.deep_dive_evidence = str(explanation)[:1200]
        if checked_reasons is not None:
            node.deep_dive_checked_reasons = list(checked_reasons)[:16]
        if verdict:
            node.deep_dive_verdict = str(verdict)
        if next_kpis is not None:
            node.deep_dive_next_kpis = [str(x) for x in next_kpis][:24]
        if edge_targets is not None:
            node.deep_dive_edge_targets = [x for x in edge_targets if isinstance(x, dict)][:16]

        # Keep deep-dive conclusion as authoritative class, but keep reason hint if already present.
        if reason_class:
            node.root_cause_reason_class = reason_class
        if conf_val >= 0.50:
            node.confidence = max(float(getattr(node, "confidence", 0.0) or 0.0), conf_val)
            if reason:
                node.reason = reason

        # Log a lightweight event in tree timeline for visibility.
        self.tree._step += 1
        self.tree._record(
            "deep_dive_update",
            node.id,
            confidence=conf_val,
            confidence_avg=float(getattr(node, "deep_dive_confidence_avg", 0.0) or 0.0),
            confidence_count=int(getattr(node, "deep_dive_confidence_count", 0) or 0),
            verdict=str(verdict or ""),
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
                # Keep localization candidates only when severity >= 50.
                if severity < 50.0:
                    if include_filtered_aux:
                        filtered_aux.append({**aux_base, "drop_reason": "low_severity"})
                    continue
                value_is_problematic = o.get("value_is_problematic")
                if isinstance(value_is_problematic, str):
                    value_is_problematic = value_is_problematic.strip().lower() in {
                        "true", "yes", "1", "problematic"
                    }
                # Temporarily disabled: do not drop localization candidates only
                # because value_is_problematic=false. Keep the annotation, but let
                # later stages decide whether it is symptom/noise/root cause.
                # if value_is_problematic is False:
                #     if include_filtered_aux:
                #         filtered_aux.append({**aux_base, "drop_reason": "not_problematic"})
                #     continue
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
        # Market pod cmdb_id often uses "node-X.<pod-name>".
        # If suffix lookup failed but this shape appears, treat it as pod first.
        if "." in comp and (comp.startswith("node-") or comp.startswith("os_")):
            return "pod"
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
