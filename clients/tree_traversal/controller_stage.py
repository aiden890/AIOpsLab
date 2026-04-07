"""Controller-driven stage: multi-turn agent ↔ actions (ReAct JSON).

Used by StagedRCAPipeline for deep_dive and expand stages.
Agent responds with JSON: thought, action, args; on conclusion
action=submit with verdict fields (e.g. confidence, explanation, other_suspect
for deep_dive; verdict, related_components, explanation for expand).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from clients.openrca_rca.api_router import get_chat_completion

logger = logging.getLogger("controller_stage")


def _parse_react_json(content: str) -> dict | None:
    """Extract a single JSON object from assistant response (raw or in code block)."""
    text = (content or "").strip()
    # Try raw JSON first (response is just the object)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Be tolerant of a few extra closing braces at the end (common LLM mistake)
    tmp = text
    for _ in range(3):
        tmp = tmp.rstrip()
        if not tmp.endswith("}"):
            break
        tmp = tmp[:-1]
        try:
            return json.loads(tmp)
        except json.JSONDecodeError:
            continue

    # Try ```json ... ``` or ``` ... ```
    for pattern in (
        r"```(?:json)?\s*\n?(.*?)```",
        r"```\s*\n?(.*?)```",
    ):
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
    # Try to recover JSON object embedded in plain text like:
    # "execute:\n{...}" or prefixed explanations.
    start_positions = [i for i, ch in enumerate(text) if ch == "{"]
    for start in start_positions:
        depth = 0
        for end in range(start, len(text)):
            ch = text[end]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start : end + 1]
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict) and (
                        "action" in obj
                        or "args" in obj
                        or "verdict" in obj
                    ):
                        return obj
                    break
    return None


def _infer_action_hint(content: str) -> str | None:
    text = str(content or "").lower()
    if '"action"' in text:
        if "submit" in text:
            return "submit"
        if "tool" in text:
            return "tool"
        if "execute" in text:
            return "execute"
    if "submit(" in text or "verdict" in text:
        return "submit"
    if "tool(" in text or "tool_name" in text:
        return "tool"
    if "execute(" in text or "instruction" in text:
        return "execute"
    return None


def _build_format_correction_message(stage_name: str, action_hint: str | None) -> str:
    tool_example = (
        '{"action":"tool","args":{"tool_name":"<tool name>",'
        '"tool_args":{"<arg>":"<value>"}}}'
    )
    execute_example = (
        '{"action":"execute","args":{"instruction":"<concrete telemetry query>",'
        '"suspected_root_cause_reason":"<allowed reason>",'
        '"kpis_to_check":["<kpi1>"],'
        '"why_these_kpis":"<anchor/supporting rationale>"}}'
    )
    submit_example = (
        '{"action":"submit","args":{"verdict":"anomaly|noise","explanation":"<short reason>",'
        '"confidence":0.0,"time":"YYYY-MM-DD HH:MM:SS","reason":"<required when anomaly>",'
        '"co_anomalous_supporting_kpis":["<kpi1>"]}}'
    )
    if action_hint == "execute":
        return (
            f"[{stage_name}] Invalid action format. "
            "Use exactly this JSON shape for execute:\n"
            f"{execute_example}"
        )
    if action_hint == "tool":
        return (
            f"[{stage_name}] Invalid action format. "
            "Use exactly this JSON shape for tool:\n"
            f"{tool_example}"
        )
    if action_hint == "submit":
        return (
            f"[{stage_name}] Invalid action format. "
            "Use exactly this JSON shape for submit:\n"
            f"{submit_example}"
        )
    return (
        f"[{stage_name}] Invalid action format. "
        "Use exactly one JSON object with top-level action + args.\n"
        f"Tool format:\n{tool_example}\n"
        f"Execute format:\n{execute_example}\n"
        f"Submit format:\n{submit_example}"
    )


def run_controller_stage(
    stage_name: str,
    system_prompt: str,
    initial_user_message: str,
    problem: Any,
    llm_configs: dict,
    parser,
    max_steps: int = 15,
    sprint=None,
    response_format: str = "react_json",
    component_level: str | None = None,
    allowed_target_components: list[str] | None = None,
    require_execute_before_submit: bool = False,
    action_validator=None,
) -> tuple[dict | None, list[dict]]:
    """Run a controller loop: LLM issues actions, we execute and append observations.

    Args:
        stage_name: e.g. "deep_dive", "expand" (for logging).
        system_prompt: System message content.
        initial_user_message: First user message.
        problem: Task with get_available_actions() and perform_action(name, *args, **kwargs).
        llm_configs: API config (SOURCE, MODEL, etc.).
        parser: ResponseParser (used only for non–ReAct code-block path).
        max_steps: Max turns.
        sprint: SessionPrint for session log; if set, log system and initial user.
        response_format: "react_json" (default) or "code_block".
        component_level: Optional logical level ("node", "pod", "service") of the
            target component for this stage. When provided, we log a warning if
            actions request a different level via component_type.

    Returns:
        (verdict, messages). verdict is the conclusion dict (e.g. confidence, explanation,
        other_suspect for deep_dive; verdict, related_components, explanation for expand),
        or None if no submit. messages is the full chat history.
    """
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_user_message},
    ]

    allowed_targets: set[str] = {
        str(x).strip() for x in (allowed_target_components or []) if str(x).strip()
    }
    # Backward-compatible fallback: parse one target component from the initial message.
    target_component: str | None = None
    if not allowed_targets:
        m = re.search(r"component='([^']+)'", initial_user_message or "")
        if m:
            target_component = m.group(1)

    if sprint:
        sprint.agent_detail(
            "[{}] System prompt (first 500 chars):\n{}".format(
                stage_name, (system_prompt or "")[:500]
            )
        )
        sprint.agent_detail(
            "[{}] Initial user message:\n{}".format(
                stage_name, initial_user_message or ""
            )
        )

    verdict: dict | None = None
    observed_once = False
    for step in range(max_steps):
        resp = get_chat_completion(messages, llm_configs, temperature=0.0)
        messages.append({"role": "assistant", "content": resp})

        if sprint:
            sprint.agent_detail(
                f"[{stage_name}] Step {step + 1}: requesting next action or verdict"
            )
            sprint.agent_detail(resp)

        if response_format == "react_json":
            parsed = _parse_react_json(resp)
            if not parsed:
                correction = _build_format_correction_message(
                    stage_name, _infer_action_hint(resp)
                )
                if sprint:
                    sprint.service_detail(correction)
                messages.append({"role": "user", "content": correction})
                continue

            action = (parsed.get("action") or "").strip().lower()
            if not action:
                correction = _build_format_correction_message(
                    stage_name, _infer_action_hint(resp)
                )
                if sprint:
                    sprint.service_detail(correction)
                messages.append({"role": "user", "content": correction})
                continue

            if not isinstance(parsed.get("args"), dict):
                correction = _build_format_correction_message(stage_name, action)
                if sprint:
                    sprint.service_detail(correction)
                messages.append({"role": "user", "content": correction})
                continue

            if action == "submit":
                if require_execute_before_submit and not observed_once:
                    obs = (
                        f"[{stage_name}] Rejected submit(): you must run execute() or tool() at least once "
                        "before concluding so the verdict is grounded in telemetry analysis."
                    )
                    if sprint:
                        sprint.service_detail(obs)
                    messages.append({"role": "user", "content": obs})
                    continue
                verdict = parsed
                if sprint:
                    sprint.service_detail(f"Verdict (ReAct): {verdict}")
                return verdict, messages

            # Execute action
            api_name = parsed.get("action") or ""
            args = parsed.get("args") or {}
            if api_name == "execute":
                instruction = args.get("instruction")
                if not instruction:
                    for key in ("instruction", "analysis", "query", "task", "prompt"):
                        value = parsed.get(key)
                        if isinstance(value, str) and value.strip():
                            args["instruction"] = value.strip()
                            break

            # Deep dive/expand: if component_type does not match candidate level, do not run the action;
            # return a warning to the controller so the agent can correct and retry.
            if component_level and args and "component_type" in args:
                comp_type = args.get("component_type")
                if comp_type and comp_type != component_level:
                    obs = (
                        f"[{stage_name}] Rejected: component_type must match the candidate's level. "
                        f"The candidate is at level {component_level!r}, but you used component_type={comp_type!r}. "
                        f"For get_kpi_peer_graph, get_trace_volume_peer_graph, get_trace_latency_peer_graph, get_trace_error_peer_graph, and get_trace_peer_graph use component_type={component_level!r}. "
                        "No chart was generated. Please retry with the correct component_type."
                    )
                    if sprint:
                        sprint.service_detail(obs)
                    messages.append({"role": "user", "content": obs})
                    continue

            # Expand stage: only allow execute() (plus submit). Any other action
            # should be rejected so the agent does not waste tokens on visualization
            # tools that it cannot see (e.g. get_kpi_peer_graph, trace peer graph actions).
            if stage_name == "expand" and api_name not in ("execute", "submit"):
                obs = (
                    f"[{stage_name}] Rejected action {api_name!r}: during {stage_name} you MUST "
                    "only use execute() to analyze telemetry tables and submit to conclude. "
                    "Do not call visualization actions like get_kpi_peer_graph, get_trace_volume_peer_graph, get_trace_latency_peer_graph, get_trace_error_peer_graph, or get_trace_peer_graph."
                )
                if sprint:
                    sprint.service_detail(obs)
                messages.append({"role": "user", "content": obs})
                continue
            if stage_name == "deep_dive" and api_name not in ("tool", "execute", "submit"):
                obs = (
                    f"[{stage_name}] Rejected action {api_name!r}: during {stage_name} you MUST "
                    "use only tool(), execute(), and submit()."
                )
                if sprint:
                    sprint.service_detail(obs)
                messages.append({"role": "user", "content": obs})
                continue
            if stage_name == "trace_expand" and api_name not in ("tool", "execute", "submit"):
                obs = (
                    f"[{stage_name}] Rejected action {api_name!r}: during {stage_name} you MUST "
                    "use only tool(), execute(), and submit()."
                )
                if sprint:
                    sprint.service_detail(obs)
                messages.append({"role": "user", "content": obs})
                continue

            if api_name == "execute" and not str(args.get("instruction") or "").strip():
                obs = (
                    f"[{stage_name}] Rejected execute(): missing required args.instruction string. "
                    'Respond like {"thought":"...","action":"execute","args":{"instruction":"<concrete telemetry analysis task>"}}.'
                )
                if sprint:
                    sprint.service_detail(obs)
                messages.append({"role": "user", "content": obs})
                continue
            if api_name == "tool" and not str(args.get("tool_name") or "").strip():
                obs = (
                    f"[{stage_name}] Rejected tool(): missing required args.tool_name string. "
                    'Respond like {"action":"tool","args":{"tool_name":"<tool name>","tool_args":{...}}}.'
                )
                if sprint:
                    sprint.service_detail(obs)
                messages.append({"role": "user", "content": obs})
                continue

            if callable(action_validator):
                try:
                    validation_msg = action_validator(parsed)
                except Exception as e:
                    validation_msg = f"[{stage_name}] Internal validator error: {e}"
                if validation_msg:
                    action_hint = None
                    vm_lower = str(validation_msg).lower()
                    if "rejected execute" in vm_lower:
                        action_hint = "execute"
                    elif "rejected tool" in vm_lower:
                        action_hint = "tool"
                    elif "rejected submit" in vm_lower:
                        action_hint = "submit"
                    if action_hint:
                        validation_msg = (
                            f"{validation_msg}\n"
                            f"{_build_format_correction_message(stage_name, action_hint)}"
                        )
                    if sprint:
                        sprint.service_detail(str(validation_msg))
                    messages.append({"role": "user", "content": str(validation_msg)})
                    continue

            # Warn if the controller passes an explicit component in args that does not
            # match allowed target components for this stage.
            if args and sprint:
                comp_arg = (
                    args.get("component")
                    or args.get("target_component")
                    or args.get("component_name")
                )
                comp_arg = str(comp_arg or "").strip()
                if comp_arg and allowed_targets and comp_arg not in allowed_targets:
                    sprint.service_detail(
                        f"[{stage_name}] Warning: action {api_name!r} uses component {comp_arg!r} "
                        f"which is outside allowed targets {sorted(allowed_targets)!r}."
                    )
                elif comp_arg and target_component and comp_arg != target_component:
                    sprint.service_detail(
                        f"[{stage_name}] Warning: target component is {target_component!r} "
                        f"but action {api_name!r} uses component {comp_arg!r} in args."
                    )

            # (component_type vs component_level mismatch is already handled above by rejecting the action.)

            try:
                action_args = args
                if api_name == "execute":
                    # execute() action only takes natural-language instruction;
                    # additional controller fields are metadata for validation/logging.
                    action_args = {"instruction": str(args.get("instruction") or "")}
                result = problem.perform_action(api_name, **action_args)
                if api_name in ("execute", "tool"):
                    observed_once = True
            except Exception as e:
                result = f"Error: {e}"
                logger.warning("perform_action(%s) failed: %s", api_name, e)

            obs = str(result)
            if sprint:
                sprint.service_detail(obs)
            messages.append({"role": "user", "content": obs})
            continue

        # code_block path: use parser to get api_name, args, then perform_action
        try:
            parsed = parser.parse(resp)
            api_name = parsed.get("api_name", "")
            args = parsed.get("args", [])
            kwargs = parsed.get("kwargs", {})
        except Exception as e:
            if sprint:
                sprint.service_detail(f"Parse error: {e}")
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Parse error: {e}. Please respond with a single code block "
                        "containing one action call."
                    ),
                }
            )
            continue

        if api_name in ("submit", "verdict"):
            verdict = {"context": parsed.get("context", "")}
            return verdict, messages

        try:
            result = problem.perform_action(api_name, *args, **kwargs)
        except Exception as e:
            result = f"Error: {e}"
        obs = str(result)
        if sprint:
            sprint.service_detail(obs)
        messages.append({"role": "user", "content": obs})

    return verdict, messages
