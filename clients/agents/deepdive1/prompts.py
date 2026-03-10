"""Prompts for Deep Dive Agent 1 (3-Stage RCA)."""

# ---------------------------------------------------------------------------
# Stage-specific prompts
# ---------------------------------------------------------------------------

EXPLORATION_PROMPT = """\
## CURRENT STAGE: EXPLORATION

You are in the EXPLORATION stage. Build a complete and reusable system map before Deep Dive.
You have a budget of **6 steps** for this stage. If you exceed the budget, you will be forced to move on.

Required task set (cover all):
1. Telemetry inventory and KPI catalog:
   - Verify all available telemetry sources/files/tables in this window (metric/trace/log).
   - Enumerate KPI names, sampling interval, and units when available.
2. Component and role inventory:
   - Identify all components found across telemetry sources.
   - Infer role/type for each component (node/pod/service/db/cache/gateway/etc).
3. Topology completeness:
   - Build call topology/call graph from traces.
   - Check whether all known components are represented in topology.
   - For missing components, state explicit reason (no traffic | missing telemetry | async/untraced).
4. Baseline readiness:
   - Build normal KPI baseline summaries at 1m, 10m, and 30m granularities.
   - Include at least median and p95 per key KPI/component group when feasible.

Completion criteria (all required):
- Confirm telemetry inventory is complete for this window.
- Confirm component inventory is complete.
- Confirm topology coverage and missing-component reasons are documented.
- Confirm component-KPI availability has been mapped.
- Confirm 1m/10m/30m baseline summaries are prepared.

When all completion criteria are satisfied, set "stage_complete": true.
When not satisfied, set "stage_complete": false.

Important:
- Even when stage_complete is true, return a valid action.
- If no additional data retrieval is needed, use execute() with a no-op instruction.

Response format:
{{"thought": "...", "action": "...", "args": {{...}}, "stage_complete": false}}
"""

DEEPDIVE_PROMPT_TEMPLATE = """\
## CURRENT STAGE: DEEP DIVE — Node [{node_id}] C={component} T={time}

You are verifying whether this (Component, Time) pair represents a REAL fault or a transient spike.
If confirmed, you must also determine the fault REASON.
You have a budget of **10 steps** for this node. If you exceed the budget, you will be forced to move on.

Verification checklist:
1. Is the KPI anomaly sustained or a brief spike?
2. Do other telemetry sources (metric + log + trace) corroborate the anomaly?
3. Are other KPIs of this component also affected?
4. Enforce temporal precedence: causes must precede or coincide with effects.
5. Enforce triangulation: validate claims with at least two independent telemetry sources when possible.
6. Reuse existing baselines/thresholds from prior steps first; only recompute if scope changes and state why.
7. Always inspect at least a ±5 minute window around candidate time T (T-5m to T+5m) before verdict.

Available reasons: {reasons}

When you have reached a conclusion, include "verdict" in your response:
- Confirmed: {{"verdict": {{"status": "CONFIRMED", "reason": "<from list>", "confidence": "high|medium|low", "evidence": "<1-line summary with specific data>"}}}}
- Rejected: {{"verdict": {{"status": "REJECTED", "reason": "", "confidence": "", "evidence": "<why rejected>"}}}}

While still investigating, set "verdict": null.

Response format:
{{"thought": "...", "action": "...", "args": {{...}}, "verdict": null}}
"""

EXPAND_PROMPT_TEMPLATE = """\
## CURRENT STAGE: EXPAND — from Node [{node_id}] C={component}

The anomaly at {component} (T={time}) has been CONFIRMED.
Now investigate whether there is a DEEPER root cause — a component that caused this fault.
You have a budget of **6 steps** for this node. If you exceed the budget, you will be forced to move on.

Search directions:
1. Upstream: trace call graph — who calls {component}? Is the caller showing an earlier anomaly?
2. Downstream: what does {component} depend on? Is a dependency failing?
3. Time precedence: did any related component show anomalies BEFORE T={time}?

When done, include "expand_result" in your response:
- Found deeper root: {{"expand_result": {{"found": true, "component": "<name>", "time": "<HH:MM or datetime>"}}}}
- No deeper root: {{"expand_result": {{"found": false, "component": "", "time": ""}}}}

While still searching, set "expand_result": null.

Response format:
{{"thought": "...", "action": "...", "args": {{...}}, "expand_result": null}}
"""

# ---------------------------------------------------------------------------
# System template
# ---------------------------------------------------------------------------

SYSTEM_TEMPLATE = """\
SERVICE MONITORING TASK — 3-STAGE DIAGNOSIS

{problem_desc}

## DIAGNOSIS RULES:

{diagnosis_rules}
{dataset_notes}
{action_list}

{possible_root_causes}\

## System Understanding
{system_understanding}

## Diagnosis Tree
{tree}

{stage_prompt}\
"""

# ---------------------------------------------------------------------------
# Action list template
# ---------------------------------------------------------------------------

ACTION_LIST_TEMPLATE = """\
## PRE-BUILT ANALYSIS ACTIONS (fast, no code needed):

{prebuilt_apis}

{execute_section}\

## SUBMIT ACTION:

{submit_api}

"""

EXECUTE_SECTION = """\
## EXECUTOR ACTION (for data retrieval and computation only):

{execute_api}

The Executor generates Python code from your instruction and runs it in a
stateful IPython kernel. Variables persist across calls — reuse them.
Provide detailed, atomic instructions. One data objective per call.
Before requesting new computations, explicitly ask to reuse already cached
DataFrames/threshold tables (e.g., prior p95 baselines) whenever applicable.
Avoid re-fetching or re-computing the same window/component unless needed.

IMPORTANT: Use execute() ONLY to fetch or compute data (metrics, traces, logs).
Do NOT use execute() to summarize, conclude, or identify root causes.
YOU (the controller) are responsible for reasoning over the results and submitting.

"""

# ---------------------------------------------------------------------------
# Summarization & force submit
# ---------------------------------------------------------------------------

SUMMARIZE_PROMPT = """\
Summarize the following diagnosis analysis into a single paragraph (3-5 sentences).
Include: component name, key metric/trace/log values with timestamps, and your conclusion.
Keep exact numbers and timestamps — do not round or omit.

Analysis to summarize:
{content}
"""

FORCE_SUBMIT_TEMPLATE = """\
You have reached the step limit. Based on your analysis so far, provide the final answer.

The candidates of possible root cause components and reasons are:
{cand}

Recall the issue: {objective}

The current diagnosis tree is:
{tree}

Submit the root cause using:
{{"thought": "...", "action": "submit", "args": {{"prediction": {{...}}}}}}
"""
