from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re

import pandas as pd

from aiopslab.orchestrator.parser import ResponseParser

from clients.tree_traversal.controller_stage import run_controller_stage
from clients.tree_traversal.tools.deep_dive.router import (
    format_deep_dive_tool_guide,
    get_deep_dive_tool_specs,
)


_COLUMN_MEANINGS = {
    "timestamp": "Unix timestamp in seconds.",
    "cmdb_id": "Component identifier. For Market pod metrics this may be node-X.pod_name.",
    "kpi_name": "Metric name.",
    "value": "Metric value for the timestamp/component/KPI row.",
    "span_id": "Unique span id.",
    "trace_id": "Trace identifier.",
    "parent_span": "Parent span id used to reconstruct caller-callee linkage.",
    "duration": "Span latency / elapsed time.",
    "status_code": "Trace status. Market success often appears as 0/OK/Ok/200/SUCCESS.",
    "type": "Trace span type/call type.",
    "operation_name": "Operation or endpoint name.",
    "message": "Log message body.",
    "level": "Log severity level.",
}


_DEEP_DIVE_V2_WORKFLOW = """\
Step 1. Review the candidate's localization evidence, available KPI catalog, and the telemetry schema/context below.

Step 2. Decide the currently most suspected root cause reason and which related KPIs should be checked next.
Use tool() or execute() to run concrete tabular analyses only.
IMPORTANT: You MUST call at least one dataset-specific tool() first.
Use execute() only when you need additional/custom analysis that predefined tools cannot provide.
In execute(), fetch per-minute telemetry tables only. Do not ask execute() to make the comparison or the judgment.
The controller should compare and summarize after reading execute() outputs, including:
- target component versus peers/siblings,
- pre-incident baseline versus incident minutes,
- and related trace/path clues only to support whether the anomaly is real.

Step 3. You MUST stay within the anomaly topic from localization.
For execute.kpis_to_check, choose KPIs from available_kpis.
focus_related_kpis is only a hint list (not a hard constraint).
reason_supporting_kpis_hints is only a hint map (not a hard constraint).

Step 4. Action format constraints:
- Every response MUST be a single JSON object with this envelope:
  {"action":"tool|execute|submit","args":{...}}

- tool action args MUST include:
  - tool_name: string
  - tool_args: object

Tool-first policy:
- Start with tool() using predefined dataset tools for minute-bucket KPI extraction.
- If needed, then call execute() for extra custom checks.
- Do not start directly with execute() unless tool catalog is empty.

- execute action args MUST include:
  - instruction: string
  - suspected_root_cause_reason: exact string from allowed reason list
  - kpis_to_check: list of KPI names, all from available_kpis
  - why_these_kpis: optional short rationale for anchor + supporting KPI selection

- submit action MUST include:
  - verdict: one of anomaly|noise
  - explanation: short reason
  - confidence: float 0.0-1.0
  - time: earliest abnormal time or original localization time
  - reason: required when verdict=anomaly, empty when verdict=noise
  - co_anomalous_supporting_kpis: list of supporting KPIs judged co-anomalous (can be empty)
  - next_kpis: optional list for extra checks

When multiple anomalous KPIs are present, you must separate:
1) primary root-cause signals (upstream cause), and
2) secondary symptom signals (downstream effect).

Do not choose reason by the largest single spike alone.
Choose reason by the strongest causal bundle across supporting KPIs.

Decision procedure (required):
- Build at least 2 competing hypotheses from observed KPIs.
- For each hypothesis, check supporting KPIs that should co-anomalize if it is the true cause.
- Prefer the hypothesis with stronger causal consistency, temporal precedence, and persistence.
- Treat isolated or short-lived co-movements as secondary symptoms unless supporting bundles confirm causality.
- If a KPI pattern repeatedly oscillates within the analysis window (recurring up/down variability without a sustained level shift), treat it as noise by default.
- Only override that default when supporting KPI bundles show a coherent causal chain with sustained degradation and problematic absolute levels.

In submit.explanation, explicitly state which KPIs are primary-cause evidence vs secondary symptoms.

Decision rule example:
- If localized KPI is `system_io_r_s` but supporting I/O health KPI such as `system.io.await` is normal, submit verdict=noise.
- If supporting KPIs are also abnormal and consistent, submit verdict=anomaly.
"""


_DEEP_DIVE_V2_SYSTEM = """\
You are the deep-dive controller for RCA.

Your job is not to expand the tree yet. Your job is to decide whether the current component is:
- a real anomaly signal for this localization topic,
- or only noise.

Allowed root cause reasons:
{possible_reasons}

Candidate context:
{candidate_context}

Available KPI catalog for this component:
{available_kpis}

Focus-related KPIs (hint only, optional to use):
{focus_related_kpis}

Reason-supporting KPI hints (optional):
{reason_supporting_kpis_hints}

Executor telemetry context:
{telemetry_context}

Dataset-specific tools:
{dataset_tool_guide}

Workflow:
{workflow}

Available actions:
{action_list}

Return JSON only, using the required envelope: {{"action":"...","args":{{...}}}}.
"""


@dataclass
class DeepDiveOutcome:
    confidence: float
    verdict: str
    reason: str | None
    time: str | None
    explanation: str
    checked_reasons: list[dict]
    next_kpis: list[str]
    edge_targets: list[dict]
    raw_verdict: dict
    messages: list[dict]


def _column_meaning_lines(columns: list[str]) -> str:
    lines = []
    for col in columns:
        meaning = _COLUMN_MEANINGS.get(col, "Column from dataset telemetry.")
        lines.append(f"- {col}: {meaning}")
    return "\n".join(lines)


def _read_csv_preview(path: Path, *, rows: int = 5) -> str:
    if not path.exists():
        return ""
    try:
        df = pd.read_csv(path, nrows=rows)
    except Exception as exc:
        return f"{path.name}: failed to read preview ({exc})"
    cols = [str(c) for c in df.columns.tolist()]
    preview = df.head(rows).to_string(index=False)
    return (
        f"[{path.name}]\n"
        f"columns: {', '.join(cols)}\n"
        f"column_meanings:\n{_column_meaning_lines(cols)}\n"
        f"head:\n{preview}\n"
    )


def build_executor_telemetry_context(actions, namespace: str) -> str:
    base_path = getattr(getattr(actions, "static_app", None), "base_path", None)
    if base_path is None:
        return "No local telemetry base path available."
    ns_path = Path(base_path) / namespace
    snippets: list[str] = []
    candidate_paths = [
        ns_path / "metrics" / "metric_node.csv",
        ns_path / "metrics" / "metric_container.csv",
        ns_path / "metrics" / "metric_service.csv",
        ns_path / "metrics" / "metric_app.csv",
        ns_path / "metrics" / "metric_mesh.csv",
        ns_path / "traces" / "trace_span.csv",
    ]
    logs_dir = ns_path / "logs"
    if logs_dir.exists():
        first_log = next(iter(sorted(logs_dir.glob("*.csv"))), None)
        if first_log is not None:
            candidate_paths.append(first_log)
    for path in candidate_paths:
        snippet = _read_csv_preview(path)
        if snippet:
            snippets.append(snippet)
    return "\n".join(snippets) if snippets else "No CSV preview available."


def _build_kpi_catalog(profile, level: str) -> list[str]:
    full_by_type = getattr(profile, "full_kpis_by_type", {}) or {}
    dataset = (profile.name or "").replace("openrca_", "")
    metric_types: list[str] = []
    if dataset.startswith("telecom"):
        if level == "node":
            metric_types = ["node"]
        elif level == "pod":
            metric_types = ["container"]
        elif level == "service":
            metric_types = ["service", "middleware"]
    elif dataset.startswith("market"):
        if level == "node":
            metric_types = ["node"]
        elif level == "pod":
            metric_types = ["container"]
        elif level == "service":
            metric_types = ["service", "mesh"]
    elif dataset.startswith("bank"):
        metric_types = ["app", "container"]
    out: list[str] = []
    seen: set[str] = set()
    for metric_type in metric_types:
        for kpi in full_by_type.get(metric_type, []) or []:
            if kpi not in seen:
                seen.add(kpi)
                out.append(kpi)
    for kpi in profile.kpis_by_type.get(level, []) or []:
        if kpi not in seen:
            seen.add(kpi)
            out.append(kpi)
    return out


def _tokenize(text: str) -> set[str]:
    return {tok for tok in re.split(r"[^a-z0-9]+", text.lower()) if len(tok) >= 3}


def _build_focus_related_kpis(node, available_kpis: list[str]) -> list[str]:
    raw_kpi_field = getattr(node, "kpi", []) or []
    if isinstance(raw_kpi_field, (str, bytes)):
        node_kpis_raw = [str(raw_kpi_field)]
    else:
        node_kpis_raw = list(raw_kpi_field)
    if not node_kpis_raw:
        hint = str(getattr(node, "reason", "") or "").strip()
        if hint:
            node_kpis_raw = [hint]
    node_kpis = [str(k).strip() for k in node_kpis_raw if str(k).strip()]
    token_pool = set()
    for item in node_kpis + [str(getattr(node, "reason", "") or "")]:
        token_pool |= _tokenize(item)
    focus = []
    seen: set[str] = set()
    for kpi in available_kpis:
        k = str(kpi).strip()
        if not k:
            continue
        if k in node_kpis:
            if k not in seen:
                seen.add(k)
                focus.append(k)
            continue
        if token_pool and (_tokenize(k) & token_pool):
            if k not in seen:
                seen.add(k)
                focus.append(k)
    for k in node_kpis:
        if k in available_kpis and k not in seen:
            seen.add(k)
            focus.append(k)
    if not focus:
        focus = available_kpis[:20]
    return focus[:40]


def _canonical_reason(reason: str, possible_reasons: list[str]) -> str | None:
    normalized = str(reason or "").strip()
    if not normalized:
        return None
    if normalized in possible_reasons:
        return normalized
    lowered = normalized.lower()
    return next((r for r in possible_reasons if r.lower() == lowered), None)


def _build_deep_dive_action_validator(
    *,
    possible_reasons: list[str],
    available_kpis: list[str],
    available_tool_names: list[str] | None = None,
):
    available_set = {str(k).strip() for k in available_kpis if str(k).strip()}
    tool_set = {str(x).strip() for x in (available_tool_names or []) if str(x).strip()}
    saw_tool = False

    def _validator(parsed: dict) -> str | None:
        nonlocal saw_tool
        action = str(parsed.get("action") or "").strip().lower()
        args = parsed.get("args")
        if not isinstance(args, dict):
            args = {}

        if action == "tool":
            tool_name = str(args.get("tool_name") or "").strip()
            if not tool_name:
                return "[deep_dive] Rejected tool(): args.tool_name is required."
            if tool_set and tool_name not in tool_set:
                return (
                    "[deep_dive] Rejected tool(): unknown tool_name. "
                    f"allowed={sorted(tool_set)!r}"
                )
            tool_args = args.get("tool_args")
            if tool_args is not None and not isinstance(tool_args, dict):
                return "[deep_dive] Rejected tool(): args.tool_args must be an object."
            saw_tool = True
        elif action == "execute":
            if tool_set and not saw_tool:
                return (
                    "[deep_dive] Rejected execute(): call at least one tool() first "
                    "with predefined dataset tools, then use execute() for extra checks."
                )
            execute_payload = args if args else parsed
            suspected_reason = str(
                execute_payload.get("suspected_root_cause_reason") or ""
            ).strip()
            if not suspected_reason:
                return (
                    "[deep_dive] Rejected execute(): args.suspected_root_cause_reason is required "
                    "and must be one of the allowed reasons."
                )
            if _canonical_reason(suspected_reason, possible_reasons) is None:
                return (
                    "[deep_dive] Rejected execute(): suspected_root_cause_reason must match one "
                    "of the allowed reasons."
                )
            raw_kpis = execute_payload.get("kpis_to_check")
            if not isinstance(raw_kpis, list) or not raw_kpis:
                return (
                    "[deep_dive] Rejected execute(): args.kpis_to_check must be a non-empty KPI list."
                )
            kpis = [str(k).strip() for k in raw_kpis if str(k).strip()]
            if not kpis:
                return (
                    "[deep_dive] Rejected execute(): args.kpis_to_check must contain valid KPI names."
                )
            if available_set:
                invalid = [k for k in kpis if k not in available_set]
                if invalid:
                    return (
                        "[deep_dive] Rejected execute(): all kpis_to_check must be in available_kpis. "
                        f"Invalid={invalid[:8]}"
                    )
        elif action == "submit":
            if tool_set and not saw_tool:
                return (
                    "[deep_dive] Rejected submit(): call at least one tool() first "
                    "before concluding."
                )
            submit_payload = args if args else parsed
            raw_verdict = str(submit_payload.get("verdict") or "").strip().lower()
            verdict = {
                "confirmed": "anomaly",
                "needs_expand": "anomaly",
                "check_edges": "anomaly",
                "symptom": "noise",
            }.get(raw_verdict, raw_verdict)
            if verdict not in {"anomaly", "noise"}:
                return "[deep_dive] Rejected submit(): verdict must be one of anomaly|noise."
            co_kpis = submit_payload.get("co_anomalous_supporting_kpis")
            if not isinstance(co_kpis, list):
                return (
                    "[deep_dive] Rejected submit(): co_anomalous_supporting_kpis is required "
                    "and must be a list (use [] when none)."
                )
            co_kpis_norm = [str(x).strip() for x in co_kpis if str(x).strip()]
            if available_set:
                invalid = [k for k in co_kpis_norm if k not in available_set]
                if invalid:
                    return (
                        "[deep_dive] Rejected submit(): co_anomalous_supporting_kpis must use KPI names "
                        f"from available_kpis. Invalid={invalid[:8]}"
                    )
            reason = str(submit_payload.get("reason") or "").strip()
            if verdict == "anomaly":
                if not reason:
                    return "[deep_dive] Rejected submit(): reason is required when verdict=anomaly."
                if _canonical_reason(reason, possible_reasons) is None:
                    return (
                        "[deep_dive] Rejected submit(): reason must match one of the allowed reasons "
                        "when verdict=anomaly."
                    )
        return None

    return _validator


_REASON_SUPPORTING_KPI_BUNDLES: dict[str, list[str]] = {
    "node disk read i/o consumption": ["system.io.r_s", "system.io.await", "system.io.util"],
    "node disk write i/o consumption": ["system.io.w_s", "system.io.await", "system.io.util"],
}


def _build_reason_supporting_kpis_hints(
    profile,
    level: str,
    possible_reasons: list[str],
    available_kpis: list[str],
) -> dict[str, list[str]]:
    available_set = {str(k).strip() for k in available_kpis if str(k).strip()}
    by_reason = dict(getattr(profile, "reason_kpis_by_level", {}).get(level, {}) or {})
    hints: dict[str, list[str]] = {}
    for reason in possible_reasons:
        canonical_reason = str(reason or "").strip()
        if not canonical_reason:
            continue
        key = canonical_reason.lower()
        curated = [
            k for k in _REASON_SUPPORTING_KPI_BUNDLES.get(key, []) if k in available_set
        ]
        if curated:
            hints[canonical_reason] = curated
            continue
        candidates = [
            str(k).strip()
            for k in (by_reason.get(canonical_reason, []) or [])
            if str(k).strip() in available_set
        ]
        dedup: list[str] = []
        seen: set[str] = set()
        for k in candidates:
            if k not in seen:
                seen.add(k)
                dedup.append(k)
        # Keep only compact/high-signal reason bundles as hard-required.
        if 2 <= len(dedup) <= 6:
            hints[canonical_reason] = dedup
    return hints


def run_deep_dive_controller(
    *,
    problem,
    actions,
    profile,
    namespace: str,
    llm_configs: dict,
    sprint,
    node,
    level: str,
    time_range: dict | None,
    normalize_time,
) -> DeepDiveOutcome:
    # Prefer dataset config reasons (possible_root_causes.reasons) when available
    # so deep-dive allowed reasons match the static dataset config as-is.
    possible_reasons = list(
        profile.possible_reasons or profile.reasons_by_level.get(level, []) or []
    )
    available_kpis = _build_kpi_catalog(profile, level)
    focus_related_kpis = _build_focus_related_kpis(node, available_kpis)
    reason_supporting_kpis_hints = _build_reason_supporting_kpis_hints(
        profile, level, possible_reasons, available_kpis
    )
    telemetry_context = build_executor_telemetry_context(actions, namespace)
    candidate_context = (
        f"component={node.component!r}\n"
        f"level={level!r}\n"
        f"localized_time={node.time!r}\n"
        f"localized_reason_hint={node.reason!r}\n"
        f"localized_kpis={json.dumps(list(getattr(node, 'kpi', []) or []), ensure_ascii=False)}\n"
        f"localized_evidence={node.evidence!r}\n"
        f"focus_related_kpis={json.dumps(focus_related_kpis, ensure_ascii=False)}\n"
    )
    if time_range and "start" in time_range and "end" in time_range:
        candidate_context += (
            f"query_window_utc=[{pd.to_datetime(float(time_range['start']), unit='s', utc=True)}, "
            f"{pd.to_datetime(float(time_range['end']), unit='s', utc=True)}]\n"
        )

    actions_desc = problem.get_available_actions() or {}
    action_list = "\n".join(
        f"- {name}: {doc[:220]}"
        for name, doc in actions_desc.items()
        if name in ("tool", "execute", "submit")
    ) or (
        "- tool: run dataset-specific tabular helpers\n"
        "- execute: run python analysis on telemetry CSV files\n"
        "- submit: finish with verdict"
    )

    if sprint:
        sprint.service_detail(
            "[Deep Dive] Available KPI catalog:\n"
            + (", ".join(available_kpis[:200]) if available_kpis else "(none)")
        )
        sprint.service_detail(
            "[Deep Dive] Executor telemetry context preview:\n"
            + (telemetry_context[:4000] if telemetry_context else "(none)")
        )

    system_prompt = _DEEP_DIVE_V2_SYSTEM.format(
        possible_reasons="\n".join(f"- {r}" for r in possible_reasons) or "- (none)",
        candidate_context=candidate_context.strip(),
        available_kpis=", ".join(available_kpis[:160]) or "(none)",
        focus_related_kpis=", ".join(focus_related_kpis[:120]) or "(none)",
        reason_supporting_kpis_hints=(
            "\n".join(
                f"- {reason}: {', '.join(kpis)}"
                for reason, kpis in reason_supporting_kpis_hints.items()
            )
            or "(none)"
        ),
        telemetry_context=telemetry_context.strip(),
        dataset_tool_guide=format_deep_dive_tool_guide(profile.name or ""),
        workflow=_DEEP_DIVE_V2_WORKFLOW.strip(),
        action_list=action_list.strip(),
    )
    initial_user_message = (
        f"Deep-dive candidate component={node.component!r} at time={node.time!r}. "
        "Decide whether this localization topic is a real anomaly or noise. "
        "Call predefined tool() first; use execute() only for additional custom checks; "
        "then submit verdict as anomaly|noise."
    )

    original_background = getattr(actions, "_background", "")
    enriched_background = (
        f"{original_background}\n\n"
        "Additional task-local telemetry previews and column meanings:\n"
        f"{telemetry_context}\n"
    )
    setattr(actions, "_background", enriched_background)
    try:
        verdict, messages = run_controller_stage(
            stage_name="deep_dive",
            system_prompt=system_prompt,
            initial_user_message=initial_user_message,
            problem=problem,
            llm_configs=llm_configs,
            parser=ResponseParser(),
            max_steps=18,
            sprint=sprint,
            response_format="react_json",
            component_level=level,
            require_execute_before_submit=True,
            action_validator=_build_deep_dive_action_validator(
                possible_reasons=possible_reasons,
                available_kpis=available_kpis,
                available_tool_names=[
                    x.name for x in get_deep_dive_tool_specs(profile.name or "")
                ],
            ),
        )
    finally:
        setattr(actions, "_background", original_background)

    verdict = verdict or {}
    verdict_payload = dict(verdict)
    if isinstance(verdict.get("args"), dict):
        # Controller often returns submit payload inside args.
        verdict_payload.update(verdict["args"])

    reason = verdict_payload.get("reason")
    if isinstance(reason, str):
        reason = reason.strip() or None
    else:
        reason = None
    raw_verdict_name = str(verdict_payload.get("verdict") or "").strip().lower()
    verdict_name = {
        "confirmed": "anomaly",
        "needs_expand": "anomaly",
        "check_edges": "anomaly",
        "symptom": "noise",
        "": "noise",
    }.get(raw_verdict_name, raw_verdict_name)
    if verdict_name not in {"anomaly", "noise"}:
        verdict_name = "noise"
    time_str = str(verdict_payload.get("time") or node.time or "").strip() or None
    if time_str:
        time_str = normalize_time(time_str) or node.time
    try:
        confidence = float(verdict_payload.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    checked_reasons = list(verdict_payload.get("checked_reasons", []) or [])
    next_kpis = [
        str(x)
        for x in (
            verdict_payload.get("next_kpis", [])
            or verdict_payload.get("kpis_to_check", [])
            or []
        )
        if str(x).strip()
    ]
    edge_targets = [x for x in (verdict_payload.get("edge_targets", []) or []) if isinstance(x, dict)]
    explanation = str(verdict_payload.get("explanation") or "")[:1200]

    if verdict_name == "noise":
        reason = None
    elif reason:
        reason = _canonical_reason(reason, possible_reasons)

    return DeepDiveOutcome(
        confidence=confidence,
        verdict=verdict_name,
        reason=reason,
        time=time_str,
        explanation=explanation,
        checked_reasons=checked_reasons,
        next_kpis=next_kpis,
        edge_targets=edge_targets,
        raw_verdict=verdict_payload,
        messages=messages,
    )
