"""Verifier agent prompts for backward RCA (three-phase evidence + rule extraction).

Phase 1: Evidence collection — agent knows ground truth, finds supporting signals.
Phase 2: Rule extraction — agent generalizes evidence into an abstract reusable rule.
Phase 3: Snippet generalization — agent rewrites critical executor code as a
         parameterized function.
"""

# ---------------------------------------------------------------------------
# Phase 1 — Evidence collection system prompt
# ---------------------------------------------------------------------------

PHASE1_SYSTEM = """You are an RCA Evidence Analyst. You already know the root cause of a system incident. Your task is to find concrete evidence in the telemetry data that proves WHY this is the root cause.

The ground truth is:
{ground_truth}

The incident context is:
{task_description}

Your job is NOT to discover the root cause — you already know it. Your job is to:
1. Use the telemetry actions to gather signals that confirm the root cause.
2. Document what each signal shows and why it proves the root cause.
3. Find the exact timestamps, component names, and metric values that are the key evidence.
4. When you have collected sufficient evidence (at least 2-3 concrete signals), mark completed=True.

In each step, your response must follow this JSON format:

{response_format}

Let's begin finding evidence."""

PHASE1_RESPONSE_FORMAT = """{
    "analysis": (Your analysis of what the telemetry data shows and how it connects to the known root cause. On the first step, respond 'Starting evidence collection.'),
    "completed": ("True" if you have found sufficient evidence (at least 2-3 signals). Otherwise "False"),
    "instruction": (Your instruction for the Executor to gather the next piece of evidence. Be specific about what to measure and why it relates to the root cause. If completed=True, summarize all evidence found.)
}
(DO NOT contain "```json" and "```" tags. DO contain the JSON object only. Use '\\n' for line breaks within strings.)"""

# ---------------------------------------------------------------------------
# Phase 2 — Rule extraction injection prompt
# ---------------------------------------------------------------------------

PHASE2_INJECTION = """You have collected evidence. Now extract a GENERALIZABLE RULE from what you found.

Think about: What abstract pattern does this incident represent? What signals would appear in ANY similar incident across different datasets or microservices?

Output a JSON rule using EXACTLY this format, wrapped in ```json tags:

```json
{{
    "rule_id": "<short_slug_describing_the_pattern>_v1",
    "root_cause_type": "<one of: resource_exhaustion | network_fault | dependency_failure | config_error | code_regression>",
    "abstract_signals": [
        "<abstract signal 1, e.g., 'resource_utilization_spike'>",
        "<abstract signal 2, e.g., 'downstream_latency_increase'>",
        "<abstract signal 3>"
    ],
    "temporal_pattern": "<causal ordering, e.g., 'resource_spike PRECEDES latency_spike by 1-3 minutes'>",
    "investigation_strategy": "<step-by-step guide for a future agent: 1. Check X. 2. Then Y. 3. Confirm with Z.>",
    "reasoning_summary": "<2-3 sentences explaining WHY this pattern causes this root cause type. Focus on the causal mechanism.>",
    "applicable_datasets": ["<dataset1>", "<dataset2>"],
    "confidence": "<low | medium | high>"
}}
```

Make the rule_id, investigation_strategy, and reasoning_summary useful to an agent that hasn't seen this specific incident.
Do NOT include specific service names, timestamps, or values — use abstract descriptions."""

# ---------------------------------------------------------------------------
# Phase 3 — Snippet generalization injection prompt
# ---------------------------------------------------------------------------

PHASE3_INJECTION = """You have extracted the rule. Now identify the SINGLE MOST CRITICAL piece of executor code from your investigation — the code that was most essential to confirming the root cause.

Rewrite it as a clean, reusable Python function that:
1. Is named exactly `run` and accepts ONLY keyword arguments
2. Returns a descriptive string with the findings
3. Has NO hardcoded values — all specific values (timestamps, component names, namespaces, metric names) become parameters
4. Uses `static_app` for data access (the object is available in the kernel)
5. Is concise (under 40 lines)

Output ONLY a Python code block in this format:

```python
def run(param1, param2, ...):
    \"\"\"One-line description of what this snippet does.\"\"\"
    # ... analysis code ...
    return "Findings: ..."
```

After the code block, on a new line, provide the snippet metadata in this exact format:
SNIPPET_METADATA:
snippet_id: <short_descriptive_id>_v1
description: <one sentence: what does this snippet find?>
when_to_use: <one sentence: when should a future agent call this? What prior action should have been done first?>
root_cause_types: <comma-separated root cause types, e.g., resource_exhaustion>
parameters:
  param1: <type and description>
  param2: <type and description>
  ..."""

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

import json
import re


def parse_phase2_rule(response: str) -> dict | None:
    """Extract rule JSON from phase 2 LLM response.

    Returns the parsed rule dict, or None if parsing fails.
    """
    # Try ```json block first
    match = re.search(r"```json\s*(.*?)\s*```", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try bare JSON object
    match = re.search(r"\{[^{}]*\"rule_id\"[^{}]*\}", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def parse_phase3_snippet(response: str) -> tuple[str | None, dict]:
    """Extract snippet code and metadata from phase 3 LLM response.

    Returns (code_str, metadata_dict). code_str is None if no code found.
    """
    # Extract Python code block
    code_match = re.search(r"```python\s*(.*?)\s*```", response, re.DOTALL)
    code = code_match.group(1).strip() if code_match else None

    # Extract SNIPPET_METADATA block
    metadata: dict = {}
    meta_match = re.search(r"SNIPPET_METADATA:(.*?)(?:\Z|```)", response, re.DOTALL)
    if meta_match:
        meta_text = meta_match.group(1)
        # Parse key: value lines
        current_key = None
        params: dict = {}
        in_params = False

        for line in meta_text.splitlines():
            line = line.rstrip()
            if not line.strip():
                continue

            # Check for "parameters:" section
            if re.match(r"\s*parameters:\s*$", line):
                in_params = True
                continue

            if in_params:
                # Parameter lines: "  param_name: description"
                param_match = re.match(r"\s{2,}(\w+):\s*(.*)", line)
                if param_match:
                    params[param_match.group(1)] = param_match.group(2).strip()
                    continue
                else:
                    in_params = False

            # Regular "key: value" line
            kv_match = re.match(r"\s*(\w+):\s*(.*)", line)
            if kv_match:
                key = kv_match.group(1)
                val = kv_match.group(2).strip()
                if key != "parameters":
                    metadata[key] = val

        if params:
            metadata["parameters"] = params

        # Parse root_cause_types into list
        if "root_cause_types" in metadata:
            rct = metadata["root_cause_types"]
            metadata["root_cause_types"] = [t.strip() for t in rct.split(",") if t.strip()]

    return code, metadata
