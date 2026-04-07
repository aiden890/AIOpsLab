from __future__ import annotations

from dataclasses import dataclass
import json

from aiopslab.orchestrator.parser import ResponseParser

from clients.tree_traversal.controller_stage import run_controller_stage
from clients.tree_traversal.telemetry_context import (
    build_dataset_format_guide,
    build_executor_telemetry_context,
)


_EXPAND_V2_WORKFLOW = """\
Step 1. Review the current search tree, current hypothesis, relation candidates, and any deep-dive edge targets.

Step 2. Use execute() iteratively to inspect:
- per-minute KPI buckets for candidate components,
- per-minute trace edge latency/error/volume where relevant,
- raw tables needed for baseline and focus-window comparison.

Use execute() only to retrieve the minute-bucket data. The controller should do the comparison and summary itself after reading the outputs, including whether each candidate has its own anomaly signal or only propagated symptoms.

Step 3. Expand is not the final judge. Its job is to decide which related components/edges deserve to become new search-tree candidates.

Step 4. When concluding, respond with action="submit" and a components list.
Every response MUST be a single JSON object with this envelope:
{"action":"execute|submit","args":{...}}

For submit, put the components list in args.components.
Each component entry should include:
- component
- time
- relation
- has_anomaly
- confidence
- anomalous_kpi
- anomaly_value
- value_is_problematic
- value_judgment
- clues

Prefer candidates that either:
- have their own KPI anomaly,
- are the most plausible edge/path predecessor,
- or were explicitly requested by deep dive edge_targets and are supported by execute() evidence.

If a candidate mostly looks like a propagated symptom or noise, do not include it.
"""


_EXPAND_V2_SYSTEM = """\
You are the expand controller for RCA.

Current hypothesis:
{hypothesis}

Current search tree:
{tree_summary}

Relation-based candidate set for this batch:
{candidate_summary}

Batch KPI checklist:
{group_kpis}

Dataset format guide:
{dataset_format_guide}

Executor telemetry context:
{telemetry_context}

Workflow:
{workflow}

Available actions:
{action_list}

Return JSON only, using the required envelope: {{"action":"...","args":{{...}}}}.
"""


@dataclass
class ExpandOutcome:
    components: list[dict]
    raw_verdict: dict


def run_expand_controller(
    *,
    problem,
    llm_configs: dict,
    sprint,
    actions,
    profile,
    namespace: str,
    hypothesis_summary: str,
    tree_summary: str,
    candidate_summary: str,
    group_kpis: list[str],
    initial_user_message: str,
) -> ExpandOutcome:
    actions_desc = problem.get_available_actions() or {}
    action_list = "\n".join(
        f"- {name}: {doc[:220]}"
        for name, doc in actions_desc.items()
        if name in ("execute", "submit")
    ) or "- execute: run telemetry analysis\n- submit: finish with component table"

    telemetry_context = build_executor_telemetry_context(actions, namespace)
    dataset_format_guide = build_dataset_format_guide(getattr(profile, "name", ""))

    system_prompt = _EXPAND_V2_SYSTEM.format(
        hypothesis=hypothesis_summary.strip(),
        tree_summary=tree_summary.strip() or "(tree empty)",
        candidate_summary=candidate_summary.strip() or "(none)",
        group_kpis=", ".join(group_kpis[:80]) if group_kpis else "(none)",
        dataset_format_guide=dataset_format_guide,
        telemetry_context=telemetry_context.strip(),
        workflow=_EXPAND_V2_WORKFLOW.strip(),
        action_list=action_list.strip(),
    )
    original_background = getattr(actions, "_background", "")
    enriched_background = (
        f"{original_background}\n\n"
        "Additional task-local telemetry previews and column meanings:\n"
        f"{telemetry_context}\n"
        "\nDataset format guide:\n"
        f"{dataset_format_guide}\n"
    )
    setattr(actions, "_background", enriched_background)
    try:
        verdict, _ = run_controller_stage(
            stage_name="expand",
            system_prompt=system_prompt,
            initial_user_message=initial_user_message,
            problem=problem,
            llm_configs=llm_configs,
            parser=ResponseParser(),
            max_steps=18,
            sprint=sprint,
            response_format="react_json",
            require_execute_before_submit=True,
        )
    finally:
        setattr(actions, "_background", original_background)
    verdict = verdict or {}
    payload = dict(verdict)
    if isinstance(verdict.get("args"), dict):
        payload.update(verdict["args"])
    components = payload.get("components")
    if not isinstance(components, list):
        components = []
    return ExpandOutcome(
        components=[x for x in components if isinstance(x, dict)],
        raw_verdict=payload,
    )
