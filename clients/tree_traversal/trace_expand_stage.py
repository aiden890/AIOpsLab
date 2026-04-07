from __future__ import annotations

from dataclasses import dataclass

from aiopslab.orchestrator.parser import ResponseParser

from clients.tree_traversal.controller_stage import run_controller_stage
from clients.tree_traversal.telemetry_context import (
    build_dataset_format_guide,
    build_executor_telemetry_context,
)
from clients.tree_traversal.tools.trace_expand.router import (
    format_trace_expand_tool_guide,
)


_TRACE_EXPAND_WORKFLOW = """\
Use a single iterative loop with tool(), execute(), and submit.

Global expand protocol:
- Step A (plan): build one inspection_plan from the full tree and target_hypotheses list.
  inspection_plan must define:
  - time_window (start/end + reason),
  - selected_hypotheses (subset to use now),
  - target_components to inspect,
  - candidate_pool_strategy (how to discover NEW expand candidates).
- Step B (verify): run tool() / execute() iteratively according to the inspection_plan.
- Step C (submit): return one or more related anomalous edge_targets candidates for next deep-dive.
  If no anomalous trace/mesh edge is found, you may submit edge_targets=[].

Guidelines:
- Prioritize trace_span/mesh dependency evidence and temporal ordering.
- Prefer NEW candidates not already over-expanded in the tree.
- Do not finalize after one weak clue; gather enough evidence.
- Select edge targets by incident-time delta, not raw level alone.
- Compare pre-incident vs post-incident windows around each hypothesis time and prefer edges with clear increases in latency/error rate near incident time.
- If an edge is consistently high across the whole window without a clear incident-time increase, treat it as baseline and do not mark it anomalous for expand.

Action format:
- Every response must be JSON envelope: {"action":"tool|execute|submit","args":{...}}
- tool args must include:
  - tool_name
  - tool_args (object)
- execute args must include:
  - instruction
  - target_component (optional but recommended)
  - relation_hint (optional)
- submit args must include:
  - edge_targets: list[dict] (multiple items are allowed)
  - used_hypotheses: list[str] (which hypotheses were used in this round)
  - inspection_plan: object (time window + targets + strategy)
  - components: optional list[dict]

edge_targets item format:
- component (required)
- causal_direction (required): upstream_cause|downstream_effect|ambiguous
- component_level (required): pod|service
- why_not_reverse (required): brief reason to reject opposite direction
- relation_hint
- why
- confidence
- time (optional)
- evidence_source (optional: trace_span|logs|mesh|metric)
- from_hypothesis_component (optional but recommended)
"""


_TRACE_EXPAND_SYSTEM = """\
You are the trace-expand controller for RCA.

Dataset:
{dataset_name}

Target hypotheses (multi):
{target_hypotheses}

Current search tree:
{tree_summary}

System structure / relation seeds:
{system_structure}

Candidate seeds:
{candidate_seeds}

Precomputed trace/mesh caller-callee dependency snapshots (seed-centered):
{precomputed_dependency_summary}

Dataset-specific verification guidance:
{dataset_probe_guidance}

Dataset format guide:
{dataset_format_guide}

Executor telemetry context:
{telemetry_context}

Dataset-specific tools:
{dataset_tool_guide}

Workflow:
{workflow}

Available actions:
{action_list}

Return JSON only, using: {{"action":"...","args":{{...}}}}.
"""


def _dataset_probe_guidance(dataset_name: str) -> str:
    name = str(dataset_name or "").lower()
    if name.startswith("openrca_market"):
        return (
            "- Prioritize trace caller/callee edge timing, then corroborate with mesh/service latency/error metrics.\n"
            "- For network-like symptoms, compare edge-level latency/error before and after the hypothesis time.\n"
            "- Pick incident-correlated increases (delta near incident time), not edges that are uniformly high across the full window.\n"
            "- If an edge stays high with little pre/post change, treat it as steady baseline (not expand anomaly).\n"
            "- Normalize pod/node naming when needed (node-X.pod -> pod)."
        )
    if name.startswith("openrca_telecom"):
        return (
            "- Prioritize trace_span call chain timing and DB/OS relation checks.\n"
            "- For network hypotheses, corroborate with ICMP/queue/tnsping-related signals.\n"
            "- Keep reasoning grounded in component-level evidence near hypothesis time."
        )
    if name.startswith("openrca_bank"):
        return (
            "- Prioritize trace + logs for transaction path failures, then corroborate with app/JVM/DB metrics.\n"
            "- Check whether upstream/downstream errors and latency shifts are temporally aligned."
        )
    return "- Use trace first, then logs/metrics to confirm relation direction and causality."


def _build_trace_expand_action_validator():
    def _validator(parsed: dict) -> str | None:
        action = str(parsed.get("action") or "").strip().lower()
        args = parsed.get("args") if isinstance(parsed.get("args"), dict) else {}
        if action == "tool":
            tool_name = str(args.get("tool_name") or "").strip()
            if not tool_name:
                return (
                    "[trace_expand] Rejected tool(): args.tool_name is required."
                )
            tool_args = args.get("tool_args")
            if tool_args is not None and not isinstance(tool_args, dict):
                return (
                    "[trace_expand] Rejected tool(): args.tool_args must be an object when provided."
                )
        if action == "submit":
            payload = args if args else parsed
            edge_targets = payload.get("edge_targets")
            if not isinstance(edge_targets, list):
                return (
                    "[trace_expand] Rejected submit(): args.edge_targets is required and must be a list."
                )
            for idx, item in enumerate(edge_targets[:64]):
                if not isinstance(item, dict):
                    return (
                        "[trace_expand] Rejected submit(): every edge_targets item must be an object. "
                        f"invalid_index={idx}"
                    )
                comp = str(item.get("component") or "").strip()
                if not comp:
                    return (
                        "[trace_expand] Rejected submit(): edge_targets[].component is required."
                    )
                causal_direction = str(item.get("causal_direction") or "").strip().lower()
                if causal_direction not in {"upstream_cause", "downstream_effect", "ambiguous"}:
                    return (
                        "[trace_expand] Rejected submit(): edge_targets[].causal_direction is required "
                        "and must be one of upstream_cause|downstream_effect|ambiguous."
                    )
                component_level = str(item.get("component_level") or "").strip().lower()
                if component_level not in {"pod", "service"}:
                    return (
                        "[trace_expand] Rejected submit(): edge_targets[].component_level is required "
                        "and must be one of pod|service."
                    )
                why_not_reverse = str(item.get("why_not_reverse") or "").strip()
                if not why_not_reverse:
                    return (
                        "[trace_expand] Rejected submit(): edge_targets[].why_not_reverse is required."
                    )
            used_hyp = payload.get("used_hypotheses")
            if not isinstance(used_hyp, list) or not [str(x).strip() for x in used_hyp if str(x).strip()]:
                return (
                    "[trace_expand] Rejected submit(): args.used_hypotheses is required and must be a non-empty list."
                )
            plan = payload.get("inspection_plan")
            if not isinstance(plan, dict):
                return (
                    "[trace_expand] Rejected submit(): args.inspection_plan is required and must be an object."
                )
        return None

    return _validator


@dataclass
class TraceExpandOutcome:
    edge_targets: list[dict]
    components: list[dict]
    raw_verdict: dict


def run_trace_expand_controller(
    *,
    problem,
    llm_configs: dict,
    sprint,
    actions,
    profile,
    namespace: str,
    dataset_name: str,
    hypothesis_summary: str,
    target_hypotheses: list[dict] | None = None,
    tree_summary: str,
    system_structure_summary: str,
    candidate_seed_summary: str,
    precomputed_dependency_summary: str = "",
    initial_user_message: str,
    parent_components: list[str] | None = None,
) -> TraceExpandOutcome:
    actions_desc = problem.get_available_actions() or {}
    action_list = "\n".join(
        f"- {name}: {doc[:220]}"
        for name, doc in actions_desc.items()
        if name in ("tool", "execute", "submit")
    ) or (
        "- tool: call dataset-specific helper tools\n"
        "- execute: run telemetry analysis\n"
        "- submit: finish with edge targets"
    )

    telemetry_context = build_executor_telemetry_context(actions, namespace)
    dataset_format_guide = build_dataset_format_guide(
        getattr(profile, "name", "") or dataset_name
    )

    target_hyp_lines: list[str] = []
    for item in (target_hypotheses or []):
        if not isinstance(item, dict):
            continue
        comp = str(item.get("component") or "").strip()
        if not comp:
            continue
        target_hyp_lines.append(
            f"- node_id={item.get('node_id')!r}, component={comp!r}, time={item.get('time')!r}, "
            f"level={item.get('level')!r}, deep_dive_verdict={item.get('deep_dive_verdict')!r}, "
            f"deep_dive_reason={item.get('deep_dive_reason')!r}, deep_dive_confidence={item.get('deep_dive_confidence')!r}"
        )
    if not target_hyp_lines:
        target_hyp_lines = [f"- {hypothesis_summary.strip() or '(none)'}"]

    system_prompt = _TRACE_EXPAND_SYSTEM.format(
        dataset_name=dataset_name,
        target_hypotheses="\n".join(target_hyp_lines),
        tree_summary=tree_summary.strip() or "(tree empty)",
        system_structure=system_structure_summary.strip() or "(none)",
        candidate_seeds=candidate_seed_summary.strip() or "(none)",
        precomputed_dependency_summary=precomputed_dependency_summary.strip() or "(none)",
        dataset_probe_guidance=_dataset_probe_guidance(dataset_name),
        dataset_format_guide=dataset_format_guide,
        telemetry_context=telemetry_context.strip(),
        dataset_tool_guide=format_trace_expand_tool_guide(dataset_name),
        workflow=_TRACE_EXPAND_WORKFLOW.strip(),
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
        seed_components = [
            x.strip() for x in str(candidate_seed_summary or "").split(",") if x.strip()
        ]
        allowed_targets = sorted(
            {
                str(c).strip()
                for c in (parent_components or [])
                if str(c).strip()
            }
            | {str(c).strip() for c in seed_components if str(c).strip()}
        )
        verdict, _ = run_controller_stage(
            stage_name="trace_expand",
            system_prompt=system_prompt,
            initial_user_message=(
                initial_user_message
                + "\n\nUse one iterative loop. You may plan/update/verify freely, then submit edge_targets."
            ),
            problem=problem,
            llm_configs=llm_configs,
            parser=ResponseParser(),
            max_steps=18,
            sprint=sprint,
            response_format="react_json",
            allowed_target_components=allowed_targets,
            require_execute_before_submit=False,
            action_validator=_build_trace_expand_action_validator(),
        )
    finally:
        setattr(actions, "_background", original_background)
    verdict = verdict or {}
    payload = dict(verdict)
    if isinstance(verdict.get("args"), dict):
        payload.update(verdict["args"])
    edge_targets = payload.get("edge_targets")
    if not isinstance(edge_targets, list):
        edge_targets = []
    components = payload.get("components")
    if not isinstance(components, list):
        components = []
    return TraceExpandOutcome(
        edge_targets=[x for x in edge_targets if isinstance(x, dict)],
        components=[x for x in components if isinstance(x, dict)],
        raw_verdict=payload,
    )
