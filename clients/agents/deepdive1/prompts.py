"""Prompts for Deep Dive Agent 1 (3-Stage RCA)."""

# ---------------------------------------------------------------------------
# Stage-specific prompts
# ---------------------------------------------------------------------------

EXPLORATION_PROMPT = """\
## CURRENT STAGE: EXPLORATION

You are in the EXPLORATION stage. Your goal is to understand the system before diving into specific candidates.

Tasks:
1. Identify all components and their types (web server, app server, DB, cache, gateway, etc.)
2. Understand the call topology (which component calls which)
3. Survey available KPI types and normal baseline levels
4. Note any system-wide anomalies visible at a high level

When you have a sufficient understanding of the system, set "stage_complete": true in your response.
Otherwise set "stage_complete": false and continue exploring.

Response format:
{{"thought": "...", "action": "...", "args": {{...}}, "stage_complete": false}}
"""

DEEPDIVE_PROMPT_TEMPLATE = """\
## CURRENT STAGE: DEEP DIVE — Node [{node_id}] C={component} T={time}

You are verifying whether this (Component, Time) pair represents a REAL fault or a transient spike.
If confirmed, you must also determine the fault REASON.

Verification checklist:
1. Is the KPI anomaly sustained or a brief spike?
2. Do other telemetry sources (metric + log + trace) corroborate the anomaly?
3. Are other KPIs of this component also affected?

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
