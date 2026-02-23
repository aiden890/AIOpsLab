"""ReAct RCA Agent with structured JSON responses.

Architecture:
  - One LLM call per step (vs 2-3 in run_rca_agent.py)
  - Structured JSON response: {"thought": "...", "action": "...", "args": {...}}
  - Agent chooses per step: cheap pre-built action OR Executor LLM
  - Executor history and kernel live in StaticRCAActionsWithExecutor, not here

Response format:
    {
        "thought": "<analysis of previous result and reasoning>",
        "action": "<action_name>",
        "args": {"param": "value", ...}
    }
"""

import json
import logging
import re
import textwrap
from pathlib import Path

import tiktoken

from clients.openrca_rca.api_router import load_config, get_chat_completion
from clients.openrca_rca.prompts.controller_prompt import rules as diagnosis_rules

logger = logging.getLogger("react_rca_agent")

# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

SYSTEM_TEMPLATE = """\
SERVICE MONITORING TASK

{problem_desc}

## DIAGNOSIS RULES:

{diagnosis_rules}

## PRE-BUILT ANALYSIS ACTIONS (fast, no code needed):

{prebuilt_apis}

{execute_section}\

## SUBMIT ACTION:

{submit_api}

{possible_root_causes}\

## WHEN TO SUBMIT:

Once you have identified the root cause component and reason with sufficient confidence, call `submit` IMMEDIATELY.
Do NOT call execute() to summarize — submit directly with your conclusion.

At each turn, respond ONLY with a valid JSON object (no markdown, no extra text):
{{
    "thought": "<your analysis of the previous result and reasoning for next step>",
    "action": "<action_name>",
    "args": {{"<param>": "<value>", ...}}
}}
"""

EXECUTE_SECTION = """\
## EXECUTOR ACTION (for custom Python analysis):

{execute_api}

The Executor LLM generates Python code from your instruction and runs it in a
stateful IPython kernel. Variables persist across calls — reuse them.
Provide detailed, atomic instructions. One analysis objective per call.

"""

RESP_INSTR = (
    "\n\nRespond with a JSON object only:\n"
    '{"thought": "...", "action": "...", "args": {...}}'
)

SUMMARY_TEMPLATE = """\
You have reached the step limit. Based on your analysis so far, provide the
final answer. The candidates of possible root cause components and reasons are:

{cand}

Recall the issue: {objective}

Review your reasoning and submit the final root cause using:
{{"thought": "...", "action": "submit", "args": {{"prediction": {{...}}}}}}
"""


def _stringify_apis(apis: dict) -> str:
    return "\n\n".join(f"{k}{v}" for k, v in apis.items())


def _format_possible_rca(prc: dict | None) -> str:
    if not prc:
        return ""
    lines = ["## POSSIBLE ROOT CAUSE CANDIDATES:\n"]
    levels = prc.get("component_levels", {})
    components = prc.get("components", [])
    reasons = prc.get("reasons", [])

    lines.append("Components:")
    if levels:
        for level, comps in levels.items():
            lines.append(f"  ({level} level)")
            lines.extend(f"  - {c}" for c in comps)
    else:
        lines.extend(f"  - {c}" for c in components)

    lines.append("\nReasons:")
    lines.extend(f"  - {r}" for r in reasons)
    return "\n".join(lines) + "\n\n"


def _extract_json(text: str) -> str:
    """Extract JSON object from LLM response, stripping markdown fences."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if match:
        return match.group(1)
    # Find outermost {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return text[start:end + 1]
    return text


def _build_action_string(action: str, args: dict) -> str:
    """Convert parsed JSON action into orchestrator action string.

    e.g. {"action": "get_log_overview", "args": {"namespace": "static-bank"}}
         → 'get_log_overview("static-bank")'
    """
    if action == "submit":
        prediction = args.get("prediction", args)
        return f'```\nsubmit({json.dumps(prediction)})\n```'

    if action == "execute":
        instruction = args.get("instruction", "")
        escaped = instruction.replace('"', '\\"')
        return f'```\nexecute("{escaped}")\n```'

    # Generic: positional args by value order
    arg_strs = []
    for v in args.values():
        if isinstance(v, str):
            arg_strs.append(f'"{v}"')
        else:
            arg_strs.append(str(v))
    return f'```\n{action}({", ".join(arg_strs)})\n```'


def _trim_history(history: list, max_tokens: int = 120_000) -> list:
    enc = tiktoken.encoding_for_model("gpt-4")
    total, trimmed = 0, []
    for msg in reversed(history):
        tokens = 4 + len(enc.encode(msg.get("content", "")))
        if total + tokens > max_tokens:
            break
        trimmed.insert(0, msg)
        total += tokens
    return trimmed


class ReactRCAAgent:
    """ReAct RCA agent with structured JSON responses.

    One LLM call per step. Chooses pre-built actions or execute() per step.
    Executor kernel and history live in StaticRCAActionsWithExecutor.
    """

    def __init__(self, api_config_path: str | None = None):
        if api_config_path is None:
            api_config_path = str(Path(__file__).parent / "api_config.yaml")
        self.configs = load_config(api_config_path)
        self.history: list[dict] = []
        self.step = 0
        self.problem_desc = ""
        self._possible_rca_cand = ""
        self._last_action: str = ""
        self._repeat_count: int = 0

    def init_context(
        self,
        problem_desc: str,
        instructions: str,
        apis: dict,
        possible_rca: dict | None = None,
    ):
        """Build system prompt, splitting apis into sections."""
        self.problem_desc = problem_desc

        execute_api   = {k: v for k, v in apis.items() if k == "execute"}
        shell_api     = {k: v for k, v in apis.items() if k == "exec_shell"}
        submit_api    = {k: v for k, v in apis.items() if k == "submit"}
        prebuilt_apis = {k: v for k, v in apis.items()
                         if k not in ("execute", "exec_shell", "submit")}
        # Merge exec_shell into prebuilt for simplicity
        prebuilt_apis.update(shell_api)

        execute_section = (
            EXECUTE_SECTION.format(execute_api=_stringify_apis(execute_api))
            if execute_api else ""
        )

        # Store for forced-submit summary
        if possible_rca:
            reasons = possible_rca.get("reasons", [])
            components = possible_rca.get("components", [])
            self._possible_rca_cand = (
                f"Components: {components}\nReasons: {reasons}"
            )

        system_content = SYSTEM_TEMPLATE.format(
            problem_desc=problem_desc,
            diagnosis_rules=diagnosis_rules,
            prebuilt_apis=_stringify_apis(prebuilt_apis),
            execute_section=execute_section,
            submit_api=_stringify_apis(submit_api),
            possible_root_causes=_format_possible_rca(possible_rca),
        )

        self.history = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": instructions},
        ]
        self.step = 0
        self._last_action = ""
        self._repeat_count = 0

    async def get_action(self, feedback: str) -> str:
        """One LLM call → parse JSON → return orchestrator action string."""
        self.step += 1

        # If the same action was repeated, nudge toward submit
        nudge = ""
        if self._repeat_count >= 1:
            nudge = (
                "\n\n[SYSTEM] You have called the same action twice in a row. "
                "You must now call `submit` with your best conclusion based on the evidence gathered so far. "
                "Do NOT repeat the same action again."
            )

        self.history.append({"role": "user", "content": feedback + RESP_INSTR + nudge})

        try:
            trimmed = _trim_history(self.history)
            response = get_chat_completion(trimmed, self.configs)
            self.history.append({"role": "assistant", "content": response})

            raw_json = _extract_json(response)
            parsed = json.loads(raw_json)

            action = parsed.get("action", "")
            args = parsed.get("args", {})
            thought = parsed.get("thought", "")

            # Track repeated actions
            action_key = f"{action}:{args.get('instruction', '')}"
            if action_key == self._last_action:
                self._repeat_count += 1
            else:
                self._last_action = action_key
                self._repeat_count = 0

            # Force submit if agent keeps repeating despite nudge
            if self._repeat_count >= 2:
                logger.warning(f"Step[{self.step}] Repeated action detected, forcing submit.")
                return self._force_submit()

            logger.info(
                f"{'-'*70}\nStep[{self.step}] Thought: {thought}\n"
                f"Action: {action}({args})\n{'-'*70}"
            )

            return _build_action_string(action, args)

        except json.JSONDecodeError:
            logger.warning(f"Step[{self.step}] JSON parse failed, retrying.")
            self.history.append({
                "role": "user",
                "content": "Your response was not valid JSON. Please respond with only a JSON object.",
            })
            try:
                response = get_chat_completion(self.history, self.configs)
                self.history.append({"role": "assistant", "content": response})
                parsed = json.loads(_extract_json(response))
                return _build_action_string(parsed.get("action", ""), parsed.get("args", {}))
            except Exception:
                return '```\nexecute("Summarize all findings so far and identify the root cause.")\n```'

        except Exception as e:
            logger.error(f"Step[{self.step}] Error: {e}")
            if "context_length_exceeded" in str(e):
                return self._force_submit()
            return '```\nexecute("Summarize all findings so far and identify the root cause.")\n```'

    def _force_submit(self) -> str:
        """Force a final submit when max steps or context limit is hit."""
        self.history.append({"role": "user", "content": SUMMARY_TEMPLATE.format(
            cand=self._possible_rca_cand,
            objective=self.problem_desc,
        )})
        try:
            response = get_chat_completion(self.history, self.configs)
            self.history.append({"role": "assistant", "content": response})
            parsed = json.loads(_extract_json(response))
            return _build_action_string(parsed.get("action", "submit"), parsed.get("args", {}))
        except Exception:
            return '```\nsubmit({})\n```'

    def cleanup(self):
        """No-op: kernel cleanup is handled by StaticRCAActionsWithExecutor."""
        pass
