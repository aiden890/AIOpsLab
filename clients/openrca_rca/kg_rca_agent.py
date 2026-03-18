"""KG RCA Agent — ReactRCAAgent without diagnosis rules.

Same as react_rca_agent.py but:
  - No diagnosis_rules in system prompt (KG context replaces them)
  - Everything else identical
"""

import base64
import json
import logging
import os
import re
from pathlib import Path

import tiktoken

from clients.openrca_rca.api_router import load_config, get_chat_completion
from clients.openrca_rca.prompts.workflow_market import (
    WORKFLOW_MARKET_WITH_VIZ,
    WORKFLOW_MARKET_WITHOUT_VIZ,
    VISION_CRITIC_MARKET,
)

logger = logging.getLogger("kg_rca_agent")

# ---------------------------------------------------------------------------
# System prompt template (no diagnosis_rules)
# ---------------------------------------------------------------------------

SYSTEM_TEMPLATE = """\
## Role Definition

You are a DevOps engineer specializing in root cause analysis (RCA) for distributed systems.
Your goal is to perform root cause analysis on the reported incident, identify the faulty component, and determine the specific reason for the failure.

## Background

{problem_desc}

{possible_root_causes}


## Workflow

Follow the steps below IN ORDER. Do NOT skip steps.

{workflow}

{action_list}

At each turn, respond ONLY with a valid JSON object (no markdown, no extra text):
{{
    "thought": "<your analysis of the previous result and reasoning for next step>",
    "action": "<action_name>",
    "args": {{"<param>": "<value>", ...}}
}}
"""


WORKFLOW_WITH_VIZ = """\
**Step 0 — Narrow down the fault time window with success rate analysis**
Use `get_success_rate_drop_graph(namespace)` to plot the app-level success rate over time. This reads metric_app.csv and highlights time windows where success rate drops below the threshold (default 95%). Use the graph to identify:
  - WHICH services experienced SR drops
  - WHEN the drops occurred (the red-shaded regions)
  - The severity of the drops (how far below threshold)
This narrows down the fault time window BEFORE scanning individual KPIs, saving significant analysis time.

**Step 1 — Scan ALL component types with peer graphs**
Use `get_kpi_peer_graph(namespace, component_type, kpi_name)` to visually scan for outliers across EVERY component type. `kpi_name` can be a single string or a list of up to 2 KPIs. You MUST check all of the following:
  - docker: container_cpu_used, container_mem_used
  - os: ICMP_ping
  - db: Sess_Connect, Session_pct, On_Off_State
After each graph, the vision critic will identify outliers. Collect all outlier components found across all types.

Do NOT use trace graphs during initial localization. Stage 1 localization must be based on metrics only.
Trace graphs are allowed only after metric-based candidates have already been localized and you are doing follow-up causal analysis or expansion.

**Step 1b — Filter outliers**
Review the collected outliers and REMOVE false positives:
  - A brief spike that appears and disappears within 1-2 minutes is transient noise, NOT a fault.
  - A component that is consistently high/low for the ENTIRE window is just its normal baseline.
  - Periodic or cyclical patterns shared with peers are normal behavior.
Only keep outliers that show a SUSTAINED behavioral change (lasting 3+ minutes) that clearly differs from peers. If no valid outlier remains for a component type after filtering, go back to Step 1 and check additional KPIs for that type.

**Step 2 — Narrow down: investigate outlier candidates**
For each remaining outlier after filtering, check additional KPIs with `get_kpi_peer_graph` or `execute()` to confirm the anomaly is real and sustained. Eliminate false positives.

**Step 3 — Identify the failure reason**
For the identified root cause component, determine the specific failure reason from the list of possible root causes.

**Step 4 — Pinpoint the exact occurrence time**
Find the exact timestamp when the anomaly first starts. Report the precise "YYYY-MM-DD HH:MM:SS" from the CSV timestamp column.

**Step 5 — Submit your conclusion**
Once you have identified the root cause component, the specific reason, and the exact occurrence time from the CSV, submit your answer. Do NOT delegate conclusion to `execute()` — that is your job.\
"""

WORKFLOW_WITHOUT_VIZ = """\
**Step 0 — Narrow down the fault time window with success rate analysis**
Use `execute()` to load metric_app.csv and compute the success rate per service over time (1-minute buckets). Identify:
  - WHICH services experienced SR drops
  - WHEN the drops occurred (exact time buckets)
  - The severity of the drops (minimum SR value)
This narrows down the fault time window BEFORE scanning individual KPIs.

**Step 1 — Scan ALL component types for anomalies**
Use `execute()` to load metric CSVs and compute per-component statistics. You MUST check all of the following:
  - docker: container_cpu_used, container_mem_used
  - os: ICMP_ping
  - db: Sess_Connect, Session_pct, On_Off_State
For each component type, compare each component's fault-window values against its baseline (pre-fault) and against peers.

Do NOT use trace data during initial localization. Stage 1 localization must be based on metrics only.
Trace analysis is allowed only after metric-based candidates have already been localized and you are doing follow-up causal analysis or expansion.

**Step 1b — Filter outliers**
Review the collected outliers and REMOVE false positives:
  - A brief spike that appears and disappears within 1-2 minutes is transient noise, NOT a fault.
  - A component that is consistently high/low for the ENTIRE window is just its normal baseline.
Only keep outliers that show a SUSTAINED behavioral change (lasting 3+ minutes) that clearly differs from peers.

**Step 2 — Narrow down: investigate outlier candidates**
For each remaining outlier, use `execute()` to check additional KPIs and confirm the anomaly is real and sustained. Eliminate false positives.

**Step 3 — Identify the failure reason**
For the identified root cause component, determine the specific failure reason from the list of possible root causes.

**Step 4 — Pinpoint the exact occurrence time**
Use `execute()` to find the exact timestamp when the anomaly first starts. Report the precise "YYYY-MM-DD HH:MM:SS" from the CSV timestamp column.

**Step 5 — Submit your conclusion**
Once you have identified the root cause component, the specific reason, and the exact occurrence time from the CSV, submit your answer. Do NOT delegate conclusion to `execute()` — that is your job.\
"""

ACTION_LIST_TEMPLATE = """\
## AVAILABLE ACTIONS:

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
    """Extract JSON object from LLM response, stripping markdown fences."""
    text = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if match:
        return match.group(1)
    start = text.find("{")
    if start == -1:
        return text
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
    if action == "submit":
        prediction = args.get("prediction", args)
        return f'```\nsubmit({json.dumps(prediction)})\n```'

    if action == "execute":
        instruction = args.get("instruction", "")
        escaped = instruction.replace('"', '\\"')
        return f'```\nexecute("{escaped}")\n```'

    arg_strs = []
    for v in args.values():
        if isinstance(v, str):
            arg_strs.append(f'"{v}"')
        else:
            arg_strs.append(str(v))
    return f'```\n{action}({", ".join(arg_strs)})\n```'


def _extract_image_paths(text: str) -> list[str]:
    """Return list of .png file paths found in *text*."""
    return [
        line.strip() for line in text.splitlines()
        if line.strip().endswith(".png") and os.path.isfile(line.strip())
    ]


def _encode_images(image_paths: list[str]) -> list[dict]:
    """Read and base64-encode images into OpenAI image_url blocks."""
    blocks = []
    for path in image_paths:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    return blocks


_VISION_CRITIC_PROMPT = """\
You are a time-series graph analysis expert.
Examine the attached peer comparison chart and identify outlier components.

## Rules
1. An outlier must show a visible CHANGE within the time window — \
a sudden spike, sharp drop, or clear shift from its own baseline. \
The anomaly is about BEHAVIOR CHANGE, not absolute level.
2. A component that is consistently high or low throughout the ENTIRE \
window is NOT an outlier — that is its normal baseline. Only flag it \
if it CHANGES (e.g., suddenly rises, drops, or diverges mid-window).
3. Regular repeating or cyclical patterns are NORMAL — do NOT flag them.
4. Minor fluctuations within the normal range are NOT outliers. \
Only flag CLEAR, OBVIOUS deviations visible at a glance.
5. It is perfectly fine to report NO outliers. Do not force-fit.

## Fault pattern reference
- CPU fault: sustained CPU spike on docker (container_cpu_used) or os (CPU_util_pct)
- network delay: os ICMP_ping sustained increase, packets still flowing
- network loss: os ICMP_ping timeout/spike + Received_packets or Sent_packets drop to zero
- db connection limit: db Session_pct rises toward 100%, Sess_Connect hits ceiling, Proc_Used_Pct rises
- db close: db Sess_Connect drops sharply, Session_pct drops, tnsping_result_time spikes

## Output format
Return ONLY a JSON object (no extra text):
{
  "outliers": [
    {"component": "<name>", "change": "<what changed and when>", "severity": "high|medium"},
    ...
  ]
}
If no outlier is found:
{"outliers": []}
"""


_SR_DROP_CRITIC_PROMPT = """\
You are a time-series graph analysis expert specializing in success rate analysis.
Examine the attached success rate chart and identify sudden drops.


## Output format
Return ONLY a JSON object (no extra text):
{
  "sr_drops": [
    {"service": "<name>", "drop_time": "<HH:MM or HH:MM:SS when drop starts>", "min_sr": "<approximate lowest SR%>", "duration": "<how long the drop lasts>"},
    ...
  ]
}
If no significant drop is found:
{"sr_drops": []}
"""


def _run_sr_drop_critic(image_blocks: list[dict], configs: dict) -> str:
    """Call vision LLM to analyze SR drop graph and return drop summary."""
    content: list[dict] = [{"type": "text", "text": _SR_DROP_CRITIC_PROMPT}]
    content.extend(image_blocks)
    try:
        result = get_chat_completion(
            [{"role": "user", "content": content}], configs,
        )
        return result or "SR drop critic: empty response."
    except Exception as e:
        logger.warning(f"SR drop critic failed: {e}")
        return f"SR drop critic error: {e}"


def _run_vision_critic(image_blocks: list[dict], configs: dict,
                       market: bool = False) -> str:
    """Call vision LLM to analyze peer graph images and return outlier summary."""
    prompt = VISION_CRITIC_MARKET if market else _VISION_CRITIC_PROMPT
    content: list[dict] = [{"type": "text", "text": prompt}]
    content.extend(image_blocks)
    try:
        result = get_chat_completion(
            [{"role": "user", "content": content}], configs,
        )
        return result or "Vision critic: empty response."
    except Exception as e:
        logger.warning(f"Vision critic failed: {e}")
        return f"Vision critic error: {e}"


def _parse_critic_top1(critic_analysis: str) -> str | None:
    """Extract the first outlier component name from critic JSON output."""
    try:
        raw = _extract_json(critic_analysis)
        parsed = json.loads(raw)
        outliers = parsed.get("outliers", [])
        if outliers and isinstance(outliers[0], dict):
            return outliers[0].get("component")
    except Exception:
        pass
    # Fallback: try old "OUTLIERS:" format
    m = re.search(r"OUTLIERS:\s*(\S+)", critic_analysis)
    if m:
        comp = m.group(1).rstrip(",")
        if comp.lower() == "none":
            return None
        return comp
    return None


def _make_multimodal_content(
    text: str, image_blocks: list[dict], critic_analysis: str,
) -> list:
    """Build multimodal content: text + critic analysis + images."""
    combined_text = (
        f"{text}\n\n"
        f"=== GRAPH ANALYSIS CRITIC ===\n{critic_analysis}\n\n"
        "The original chart is attached below. Verify the critic's findings "
        "with your own eyes, then decide your next action."
        f"{RESP_INSTR}"
    )
    content: list[dict] = [{"type": "text", "text": combined_text}]
    content.extend(image_blocks)
    return content


def _content_token_count(content, enc) -> int:
    """Estimate token count for plain-text or multimodal content."""
    if isinstance(content, str):
        return 4 + len(enc.encode(content))
    # multimodal list
    text = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
    n_images = sum(1 for p in content if p.get("type") == "image_url")
    return 4 + len(enc.encode(text)) + n_images * 1000


def _trim_history(history: list, max_tokens: int = 120_000) -> list:
    enc = tiktoken.encoding_for_model("gpt-4")
    total, trimmed = 0, []
    for msg in reversed(history):
        tokens = _content_token_count(msg.get("content", ""), enc)
        if total + tokens > max_tokens:
            break
        trimmed.insert(0, msg)
        total += tokens
    return trimmed


class KGRCAAgent:
    """KG RCA agent — same as ReactRCAAgent but without diagnosis rules."""

    def __init__(self, api_config_path: str | None = None):
        if api_config_path is None:
            api_config_path = str(Path(__file__).parent / "api_config.yaml")
        self.configs = load_config(api_config_path)
        self.history: list[dict] = []
        self.step = 0
        self.problem_desc = ""
        self._possible_rca_cand = ""
        self._last_action = ""  # tracks which action produced the current feedback
        self.sprint = None  # set externally for logging

    def init_context(
        self,
        problem_desc: str,
        instructions: str,
        apis: dict,
        possible_rca: dict | None = None,
        dataset_type: str | None = None,
    ):
        self.problem_desc = problem_desc
        self.dataset_type = dataset_type or ""

        execute_api   = {k: v for k, v in apis.items() if k == "execute"}
        shell_api     = {k: v for k, v in apis.items() if k == "exec_shell"}
        submit_api    = {k: v for k, v in apis.items() if k == "submit"}
        prebuilt_apis = {k: v for k, v in apis.items()
                         if k not in ("execute", "exec_shell", "submit")}
        prebuilt_apis.update(shell_api)

        execute_section = (
            EXECUTE_SECTION.format(execute_api=_stringify_apis(execute_api))
            if execute_api else ""
        )

        if possible_rca:
            reasons = possible_rca.get("reasons", [])
            components = possible_rca.get("components", [])
            self._possible_rca_cand = (
                f"Components: {components}\nReasons: {reasons}"
            )

        action_content = ACTION_LIST_TEMPLATE.format(
            prebuilt_apis=_stringify_apis(prebuilt_apis),
            execute_section=execute_section,
            submit_api=_stringify_apis(submit_api),
        )
        self.action_content = action_content

        # Choose workflow based on dataset type and visualization availability
        has_viz = any(k.endswith("_graph") for k in apis)
        is_market = self.dataset_type.startswith("market")
        if is_market:
            workflow = WORKFLOW_MARKET_WITH_VIZ if has_viz else WORKFLOW_MARKET_WITHOUT_VIZ
        else:
            workflow = WORKFLOW_WITH_VIZ if has_viz else WORKFLOW_WITHOUT_VIZ

        system_content = SYSTEM_TEMPLATE.format(
            problem_desc=problem_desc,
            workflow=workflow,
            action_list=action_content,
            possible_root_causes=_format_possible_rca(possible_rca),
        )

        self.history = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": instructions},
        ]
        self.step = 0

    async def get_action(self, feedback: str) -> str:
        self.step += 1

        image_paths = _extract_image_paths(feedback)
        if image_paths:
            image_blocks = _encode_images(image_paths)

            # Use SR drop critic for get_success_rate_drop_graph, KPI critic otherwise
            if self._last_action == "get_success_rate_drop_graph":
                critic_analysis = _run_sr_drop_critic(image_blocks, self.configs)
                critic_label = "SR DROP CRITIC"
            else:
                is_market = self.dataset_type.startswith("market")
                critic_analysis = _run_vision_critic(
                    image_blocks, self.configs, market=is_market,
                )
                critic_label = "VISION CRITIC"

            logger.info(f"Step[{self.step}] {critic_label}: {critic_analysis}")

            # Log critic result to session log via sprint
            if self.sprint:
                top1 = _parse_critic_top1(critic_analysis)
                self.sprint._log(
                    f"\n{'='*60}\n"
                    f"[{critic_label}] Step {self.step}\n"
                    f"  Images: {', '.join(image_paths)}\n"
                    f"  Result: {critic_analysis}\n"
                    f"  Top-1 outlier: {top1 or 'none'}\n"
                    f"{'='*60}"
                )

            content = _make_multimodal_content(
                feedback, image_blocks, critic_analysis,
            )
        else:
            content = feedback + RESP_INSTR

        self.history.append({"role": "user", "content": content})

        response = None
        max_retries = 3
        for _ in range(max_retries):
            try:
                trimmed = _trim_history(self.history)
                response = get_chat_completion(trimmed, self.configs)

                if response is None:
                    logger.warning(f"Step[{self.step}] API returned None, retrying.")
                    continue

                # Strip images from history after LLM has seen them.
                # The critic analysis text is already embedded, so images
                # are no longer needed and would bloat the context.
                self._strip_images_from_last_user_msg()

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

                self._last_action = action
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

    def _strip_images_from_last_user_msg(self):
        """Replace the last user multimodal content with text-only version.

        After the LLM has processed the image, we keep only the text parts
        (which include the critic analysis) to avoid bloating history.
        """
        for msg in reversed(self.history):
            if msg["role"] == "user" and isinstance(msg["content"], list):
                text = "\n".join(
                    p["text"] for p in msg["content"] if p.get("type") == "text"
                )
                msg["content"] = text
                break

    def _force_submit(self) -> str:
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
        return self.configs.get("MODEL", "unknown")

    def cleanup(self):
        pass
