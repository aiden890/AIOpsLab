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
    return None


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

    # Try to parse target component from the initial user message so that
    # we can later warn if actions use a different component in args.
    target_component: str | None = None
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
                if sprint:
                    sprint.service_detail(
                        "Could not parse response as JSON. "
                        "Respond with a single JSON object only, e.g. "
                        '{"thought": "...", "action": "get_kpi_peer_graph", "args": {...}} '
                        'or {"action": "submit", "confidence": 0.8, "explanation": "..."}.'
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your last message was not valid JSON. "
                            "Respond with exactly one JSON object containing thought, action, and args; "
                            'when concluding use action "submit" and include the required verdict fields.'
                        ),
                    }
                )
                continue

            action = (parsed.get("action") or "").strip().lower()
            if action == "submit":
                verdict = parsed
                if sprint:
                    sprint.service_detail(f"Verdict (ReAct): {verdict}")
                return verdict, messages

            # Execute action
            api_name = parsed.get("action") or ""
            args = parsed.get("args")
            if not isinstance(args, dict):
                args = {}

            # Deep dive/expand: if component_type does not match candidate level, do not run the action;
            # return a warning to the controller so the agent can correct and retry.
            if component_level and args and "component_type" in args:
                comp_type = args.get("component_type")
                if comp_type and comp_type != component_level:
                    obs = (
                        f"[{stage_name}] Rejected: component_type must match the candidate's level. "
                        f"The candidate is at level {component_level!r}, but you used component_type={comp_type!r}. "
                        f"For get_kpi_peer_graph and get_trace_peer_graph use component_type={component_level!r}. "
                        "No chart was generated. Please retry with the correct component_type."
                    )
                    if sprint:
                        sprint.service_detail(obs)
                    messages.append({"role": "user", "content": obs})
                    continue

            # Deep dive / expand stage: only allow execute() (plus submit). Any other action
            # should be rejected so the agent does not waste tokens on visualization
            # tools that it cannot see (e.g. get_kpi_peer_graph, get_trace_peer_graph).
            if stage_name in ("deep_dive", "expand") and api_name not in ("execute", "submit"):
                obs = (
                    f"[{stage_name}] Rejected action {api_name!r}: during {stage_name} you MUST "
                    "only use execute() to analyze telemetry tables and submit to conclude. "
                    "Do not call visualization actions like get_kpi_peer_graph or get_trace_peer_graph."
                )
                if sprint:
                    sprint.service_detail(obs)
                messages.append({"role": "user", "content": obs})
                continue

            # Warn if the controller passes an explicit component in args that does not
            # match the target component of this stage.
            if target_component and args and sprint:
                comp_arg = (
                    args.get("component")
                    or args.get("target_component")
                    or args.get("component_name")
                )
                if comp_arg and comp_arg != target_component:
                    sprint.service_detail(
                        f"[{stage_name}] Warning: target component is {target_component!r} "
                        f"but action {api_name!r} uses component {comp_arg!r} in args."
                    )

            # (component_type vs component_level mismatch is already handled above by rejecting the action.)

            try:
                result = problem.perform_action(api_name, **args)
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

