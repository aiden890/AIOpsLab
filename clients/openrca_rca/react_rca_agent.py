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
from pathlib import Path

import tiktoken

from clients.openrca_rca.api_router import load_config, get_chat_completion
from clients.openrca_rca.prompts.controller_prompt import rules as diagnosis_rules

logger = logging.getLogger("react_rca_agent")

# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

_TRACE_LOCALIZATION_STEP = """\
- **After identifying faulty components via metrics, call `get_trace_call_graph()` to find the deepest faulty leaf node before saving a hypothesis:**

  `get_trace_call_graph(namespace, start_time, end_time, faulty_components=[...])` automatically:
  - Computes callee signal (A): fail_rate when called as dsName → db/service fault
  - Computes caller signal (B): elapsed_ratio vs peer components of the same type → pod/node fault
  - Returns the best candidate component, fault datetime, and ranked scores

  Steps:
  1. Pass the faulty components list from `get_kpi_high_deviation()` / `get_kpi_low_deviation()` as `faulty_components`
  2. Interpret the result by signal type:
     - `callee_score > 0` → the component is failing as a server → it IS the root cause candidate
     - `callee_score = 0.00`, `caller_score` high → the component is slow AS A CLIENT (victim of something it calls) → do NOT use it as the root cause; instead investigate its downstream callees
  3. **For network-related faults (packet loss / latency): KPI metrics alone are often insufficient.**
     Use `execute()` to examine the latency relationship between parent spans and child spans in traces:
     - Reconstruct parent-child pairs via `parent_id` → `span_id` join
     - If a parent span's duration is much larger than the sum of its children's durations, the gap is transport/network time → indicates network latency or packet loss at that component
     - The component whose spans consistently show this gap is the network fault origin
  4. Use the returned fault datetime (in UTC) as the fault occurrence time in `save_hypothesis()`
"""

def _build_hypothesis_section(use_trace: bool) -> str:
    trace_step = _TRACE_LOCALIZATION_STEP if use_trace else (
        "- Use the component with the strongest metric deviation as the candidate for that level\n"
    )
    return f"""\
## HYPOTHESIS TRACKING:

Follow this 3-phase process to systematically cover all root cause candidates:

### Phase 1 — One candidate per component level:
Investigate each component level independently and save the **best candidate** from each level.
For each level (node / pod / service), find the most anomalous component and save it:
{trace_step}
- Save the top suspect from each level with a specific reason

### Phase 2 — One candidate per possible reason:
For each reason in the possible reasons list, assess which component most likely caused it:
- Look for the metric pattern, trace signature, or log error that matches that reason type
- Even if confidence is low, save the best-matching component + reason pair
- This ensures every reason type is considered before narrowing down

### Phase 3 — Relate hypotheses and submit:
After covering all component levels and all reason types:
1. Call `get_hypothesis_causal_graph(namespace, start_time, end_time, components=[...])` to see the trace
   call chain connecting all hypothesis components, with avg elapsed time and network gap
   per edge. Pass `components` to include any additional candidates you want to compare
   alongside the saved hypotheses (e.g., components suspected from KPI analysis but not yet saved).
   This shows the fault propagation direction visually.
2. Call `analyze_hypothesis_relationships("<instruction>")` — MANDATORY before submit.
   In the instruction, use the causal graph result to ask:
   "For each hypothesis pair, determine if one fault caused the other. Write the full
   propagation chain. Then identify the root origin using these rules:
   - **Independence ≠ root cause.** A component with no other hypothesis explaining it
     is just 'unexplained by saved hypotheses' — it may still be a background fault
     unrelated to the service degradation. Do NOT select it as root cause solely because
     it is independent.
   - **Severity on the service call path matters.** Among candidate root causes, prefer
     the component with the LARGEST elapsed time or network gap (from the causal graph)
     AND that lies on the critical service call chain (components directly handling
     requests from clients/load balancers).
   - **Background vs. on-path faults:** A database (Redis/Mysql) showing memory pressure
     independently is often a background fault — it may not be the origin of the service
     degradation. A middleware/app-tier component (IG, MG, Tomcat) with very high elapsed
     time (e.g. >500ms) and large network gap (>30%) on the critical path is a stronger
     root cause candidate for service-level incidents.
   - Conclude: which component's fault, if removed, would most likely restore service?"
3. After reviewing the relationship analysis result, submit the single root origin hypothesis

**Confidence guidelines:**
- `high`: Component shows clear anomaly above threshold and reason pattern matches directly
- `medium`: Trace or log points to component, but metric evidence is indirect
- `low`: Component is suspicious but evidence is weak or circumstantial

**Every hypothesis MUST have a specific reason from the possible reasons list.**
Do NOT save a hypothesis with an unknown, unclear, or placeholder reason.
If you cannot determine the reason, investigate further:
- Analyze metric KPI patterns for that component (CPU, memory, network, disk)
- Check trace error codes and elapsed times for calls to/from that component
- Search logs for error messages or warnings from that component

**Fault datetime MUST be sourced from actual telemetry data — never estimated or guessed.**
Use the exact timestamp read from one of:
- Metric: use `peak_high_ts` from `get_kpi_high_deviation()` or `peak_low_ts` from `get_kpi_low_deviation()` — these are the exact timestamps of the maximum/minimum KPI value in the fault window
- Trace: the `startTime` or `timestamp` field of the first error/high-latency span for that component
- Log: the `timestamp` field of the first error/warning log entry for that component
Always quote the source in the evidence string, e.g.:
  "metric: os_012 CPU_util first exceeded P95 at 2020-05-22 16:48:23 (from metric_node.csv)"
  "trace: db_003 span error at startTime=2020-05-22 16:47:55 (from trace_span.csv)"

"""

SYSTEM_TEMPLATE = """\
SERVICE MONITORING TASK

{problem_desc}

## DIAGNOSIS RULES:

{diagnosis_rules}
{dataset_notes}
{action_list}

{possible_root_causes}\
{hypothesis_section}
## WHEN TO SUBMIT:

After receiving data from execute() or pre-built actions, YOU analyze the results and draw conclusions yourself.
{submit_hypothesis_note}Do NOT delegate summarization or conclusion to execute() — that is your job as the controller.

At each turn, respond ONLY with a valid JSON object (no markdown, no extra text):
{{
    "thought": "<your analysis of the previous result and reasoning for next step>",
    "action": "<action_name>",
    "args": {{"<param>": "<value>", ...}}
}}
"""


ACTION_LIST_TEMPLATE = """\
## PRE-BUILT ANALYSIS ACTIONS (fast, no code needed, you can modify start time and end time if you want for rca):

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
    """Extract JSON object from LLM response, stripping markdown fences.

    Uses brace-balancing so a trailing extra } from the LLM doesn't break parsing.
    """
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if match:
        return match.group(1)
    # Find the start of the JSON object
    start = text.find("{")
    if start == -1:
        return text
    # Walk forward counting balanced braces (respecting strings)
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:]


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

    def init_context(
        self,
        problem_desc: str,
        instructions: str,
        apis: dict,
        possible_rca: dict | None = None,
        dataset_notes: str | None = None,
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

        self.action_content = ACTION_LIST_TEMPLATE.format(
            prebuilt_apis=_stringify_apis(prebuilt_apis),
            execute_section=execute_section,
            submit_api=_stringify_apis(submit_api),
        )
        use_hypothesis = "save_hypothesis" in apis
        use_trace = any(k in apis for k in ("get_traces", "get_trace_summary"))
        use_relationship_analysis = "analyze_hypothesis_relationships" in apis
        use_causal_graph = "get_hypothesis_causal_graph" in apis
        if use_hypothesis and use_relationship_analysis:
            submit_hypothesis_note = (
                "Follow the 3-phase hypothesis process: cover all component levels, "
                "cover all possible reasons, then call `get_hypothesis_causal_graph()` "
                "and `analyze_hypothesis_relationships()` to reason about the fault chain, "
                "and submit the root origin. "
                "Do NOT submit until both have been called.\n"
            )
        elif use_hypothesis:
            submit_hypothesis_note = (
                "Follow the 3-phase hypothesis process: cover all component levels, "
                "cover all possible reasons, then call `get_hypotheses()` and submit "
                "the strongest candidate. Do NOT submit until you have compared all candidates.\n"
            )
        else:
            submit_hypothesis_note = (
                "Once you have identified the root cause, call `submit` immediately.\n"
            )
        system_content = SYSTEM_TEMPLATE.format(
            problem_desc=problem_desc,
            diagnosis_rules=diagnosis_rules,
            dataset_notes=("\n" + dataset_notes.strip() + "\n") if dataset_notes else "",
            action_list=self.action_content,
            possible_root_causes=_format_possible_rca(possible_rca),
            hypothesis_section=_build_hypothesis_section(use_trace) if use_hypothesis else "",
            submit_hypothesis_note=submit_hypothesis_note,
        )

        self.history = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": instructions},
        ]
        self.step = 0

    async def get_action(self, feedback: str) -> str:
        """One LLM call → parse JSON → return orchestrator action string."""
        self.step += 1

        self.history.append({"role": "user", "content": feedback + RESP_INSTR})

        response = None
        max_retries = 3
        for _ in range(max_retries):
            try:
                trimmed = _trim_history(self.history)
                response = get_chat_completion(trimmed, self.configs)

                if response is None:
                    logger.warning(f"Step[{self.step}] API returned None, retrying.")
                    continue

                self.history.append({"role": "assistant", "content": response})

                raw_json = _extract_json(response)
                parsed = json.loads(raw_json)

                action = parsed.get("action", "")
                args = parsed.get("args", {})
                thought = parsed.get("thought", "")

                logger.info(
                    f"{'-'*70}\nStep[{self.step}] Thought: {thought}\n"
                    f"Action: {action}({args})\n{'-'*70}"
                )

                action_str = _build_action_string(action, args)
                return f"Thought: {thought}\n{action_str}" if thought else action_str

            except json.JSONDecodeError:
                logger.warning(f"Step[{self.step}] JSON parse failed, retrying.")
                logger.error(f"Response: {response}")
                self.history.append({
                    "role": "user",
                    "content": "Your response was not valid JSON. Please respond with only a JSON object.",
                })
                continue

            except Exception as e:
                logger.error(f"Step[{self.step}] Error: {e}")
                self.history.append({"role": "user", "content": f"Your response got an error: {e}. Please try again. {self.action_content}"})
                logger.error(f"Response: {response}")
                continue

        if response is None:
            logger.warning(f"Step[{self.step}] API returned None after all retries, skipping step.")
            return ""

        return self._force_submit()



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

    def get_model_name(self) -> str:
        """Return the controller LLM model name for save path labeling."""
        return self.configs.get("MODEL", "unknown")

    def cleanup(self):
        """No-op: kernel cleanup is handled by StaticRCAActionsWithExecutor."""
        pass
