"""RCA Agent with Critic validation.

Based on the original OpenRCA controller.py architecture with a Critic layer
inserted between the Controller's analysis and instruction phases.

Flow per step:
  1. Controller generates analysis (observation of executor result)
  2. Critic validates analysis against raw executor result
  3. Controller generates instruction using the validated analysis
  4. Executor runs the instruction → result fed back to step 1

Response format matches original OpenRCA:
  - Phase 1: {"analysis": "..."}
  - Phase 3: {"instruction": "...", "completed": "True/False"}
"""

import json
import logging
import re
from pathlib import Path

import tiktoken

from clients.openrca_rca.api_router import load_config, get_chat_completion
from clients.openrca_rca.prompts.critic_prompt import (
    CRITIC_SYSTEM_PROMPT,
    CRITIC_USER_TEMPLATE,
)
from clients.openrca_rca.prompts.controller_prompt import rules as diagnosis_rules

logger = logging.getLogger("react_rca_critic_agent")

# ---------------------------------------------------------------------------
# System prompt: matches original OpenRCA controller.py format
# ---------------------------------------------------------------------------

SYSTEM_TEMPLATE = """\
You are the Administrator of a DevOps Assistant system for failure diagnosis. \
To solve each given issue, you should iteratively instruct an Executor to write \
and execute Python code for data analysis on telemetry files of target system. \
By analyzing the execution results, you should approximate the answer step-by-step.

There is some domain knowledge for you:

{background}

{rules}

The issue you are going to solve is:

{objective}

Solve the issue step-by-step. Your analysis and instruction will be requested separately."""

# Phase 1: ask for analysis only
ANALYSIS_NOTE = """\
Continue your reasoning process for the target issue.

Follow the rules during issue solving.

Analyze the execution result from Executor. Respond with ONLY a JSON object:
{{"analysis": "(Your analysis of the code execution result, with detailed reasoning of \
'what have been done' and 'what can be derived'. Respond 'None' if it is the first step.)"}}
(DO NOT contain "```json" and "```" tags. DO contain the JSON object with the brackets "{{}}" only.)"""

# Phase 3: ask for instruction only (after critic validation)
INSTRUCTION_NOTE = """\
Based on the validated analysis above, provide your next instruction for the Executor.

Respond with ONLY a JSON object:
{{"completed": ("True" if you believe the issue is resolved. Otherwise "False"),
"instruction": "(Your instruction for the Executor to perform via code execution. \
Keep your instruction atomic, with clear request of 'what to do' and 'how to do'. \
Respond a summary by yourself if you believe the issue is resolved.)"}}
(DO NOT contain "```json" and "```" tags. DO contain the JSON object with the brackets "{{}}" only. \
Use '\\n' instead of an actual newline character to ensure JSON compatibility.)"""

# Force submit summary (matches original OpenRCA controller.py summary)
SUMMARY_TEMPLATE = """\
Now, the maximum steps of your reasoning have been reached. You should now provide \
the final answer to the issue. The candidates of possible root cause components and \
reasons are provided to you. The root cause components and reasons must be selected \
from the provided candidates.

{cand}

Recall the issue is: {objective}

Please first review your previous reasoning process to infer an exact answer of the \
issue. Then, summarize your final answer of the root causes using the following JSON \
format at the end of your response:

```json
{{
    "1": {{
        "root cause occurrence datetime": (if asked by the issue, format: '%Y-%m-%d %H:%M:%S', otherwise ommited),
        "root cause component": (if asked by the issue, one selected from the possible root cause component list, otherwise ommited),
        "root cause reason": (if asked by the issue, one selected from the possible root cause reason list, otherwise ommited),
    }}, (mandatory)
    "2": {{
        "root cause occurrence datetime": (if asked by the issue, format: '%Y-%m-%d %H:%M:%S', otherwise ommited),
        "root cause component": (if asked by the issue, one selected from the possible root cause component list, otherwise ommited),
        "root cause reason": (if asked by the issue, one selected from the possible root cause reason list, otherwise ommited),
    }}, (only if the failure number is "unknown" or "more than one" in the issue)
    ... (only if the failure number is "unknown" or "more than one" in the issue)
}}
```
(Please use "```json" and "```" tags to wrap the JSON object. You only need to provide \
the elements asked by the issue, and ommited the other fields in the JSON.)
Note that all the root cause components and reasons must be selected from the provided \
candidates. Do not reply 'unknown' or 'null' or 'not found' in the JSON. Do not be too \
conservative in selecting the root cause components and reasons. Be decisive to infer a \
possible answer based on your current observation."""


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


class ReactRCACriticAgent:
    """RCA agent with Critic validation, using original OpenRCA prompt format.

    Only uses execute() for telemetry access and submit() for final answer.
    No pre-built actions or hypothesis tracking.
    """

    def __init__(self, api_config_path: str | None = None):
        if api_config_path is None:
            api_config_path = str(Path(__file__).parent / "api_config.yaml")
        self.configs = load_config(api_config_path)
        self.history: list[dict] = []
        self.step = 0
        self.problem_desc = ""
        self._cand = ""
        self._critic_trajectory: list[dict] = []

    # ------------------------------------------------------------------
    # init_context: original OpenRCA format (background + rules + objective)
    # ------------------------------------------------------------------

    def init_context(
        self,
        problem_desc: str,
        instructions: str,
        apis: dict,
        possible_rca: dict | None = None,
        dataset_notes: str | None = None,
    ):
        """Build system prompt matching original OpenRCA controller.py format.

        Only execute() and submit() are exposed to the agent.
        """
        self.problem_desc = problem_desc

        # Store candidates for force-submit
        if possible_rca:
            reasons = possible_rca.get("reasons", [])
            components = possible_rca.get("components", [])
            self._cand = f"Components: {components}\nReasons: {reasons}"

        # background = basic_prompt schema (passed via dataset_notes or from init)
        # The schema is already embedded in problem_desc by the orchestrator,
        # but we also include dataset_notes if provided
        background = dataset_notes.strip() if dataset_notes else ""

        system_content = SYSTEM_TEMPLATE.format(
            background=background,
            rules=diagnosis_rules,
            objective=problem_desc,
        )

        self.history = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "Let's begin."},
        ]
        self.step = 0

    # ------------------------------------------------------------------
    # Main loop: analysis → critic → instruction
    # ------------------------------------------------------------------

    async def get_action(self, feedback: str) -> str:
        """Three-phase step: analysis → critic → instruction."""
        self.step += 1

        # Phase 1: Controller generates analysis
        analysis = self._generate_analysis(feedback)
        if analysis is None:
            return self._force_submit()

        # Skip critic on first step (no executor result to validate against)
        if self.step == 1:
            final_analysis = analysis
            logger.info(f"Step[{self.step}] Skipping critic (first step)")
        else:
            # Phase 2: Critic validates analysis against raw feedback
            critique = self._run_critic(raw_feedback=feedback, analysis=analysis)

            if critique["has_issues"]:
                final_analysis = critique["revised_observation"] or analysis
                logger.info(
                    f"Step[{self.step}] Critic revised analysis "
                    f"({len(critique['issues'])} issues)"
                )
                for issue in critique["issues"]:
                    logger.info(f"  - {issue.get('type')}: {issue.get('detail')}")
            else:
                final_analysis = analysis
                logger.info(f"Step[{self.step}] Critic: no issues")

        # Phase 3: Controller generates instruction
        return self._generate_instruction(final_analysis)

    # ------------------------------------------------------------------
    # Phase 1: Analysis (observation)
    # ------------------------------------------------------------------

    def _generate_analysis(self, feedback: str) -> str | None:
        """Ask Controller to analyze the executor result (no instruction yet)."""
        self.history.append({
            "role": "user",
            "content": feedback + "\n\n" + ANALYSIS_NOTE,
        })

        for _ in range(3):
            try:
                trimmed = _trim_history(self.history)
                response = get_chat_completion(trimmed, self.configs)
                if response is None:
                    logger.warning(f"Step[{self.step}] Analysis: API returned None")
                    continue

                self.history.append({"role": "assistant", "content": response})

                raw_json = _extract_json(response)
                parsed = json.loads(raw_json)
                analysis = parsed.get("analysis", response)

                logger.info(f"Step[{self.step}] Analysis: {analysis[:200]}...")
                return analysis

            except json.JSONDecodeError:
                logger.warning(f"Step[{self.step}] Analysis JSON parse failed")
                self.history.append({
                    "role": "user",
                    "content": 'Please provide your analysis in JSON format: {"analysis": "..."}',
                })
            except Exception as e:
                logger.error(f"Step[{self.step}] Analysis error: {e}")
                break

        return None

    # ------------------------------------------------------------------
    # Phase 2: Critic
    # ------------------------------------------------------------------

    def _run_critic(self, raw_feedback: str, analysis: str) -> dict:
        """Validate analysis against raw executor result using the Critic LLM."""
        critic_messages = [
            {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
            {"role": "user", "content": CRITIC_USER_TEMPLATE.format(
                raw_result=raw_feedback,
                observation=analysis,
            )},
        ]

        result = {"has_issues": False, "issues": [], "revised_observation": None}

        try:
            response = get_chat_completion(critic_messages, self.configs)
            if response is None:
                logger.warning(f"Step[{self.step}] Critic: API returned None")
            else:
                raw_json = _extract_json(response)
                parsed = json.loads(raw_json)
                result = {
                    "has_issues": parsed.get("has_issues", False),
                    "issues": parsed.get("issues", []),
                    "revised_observation": parsed.get("revised_observation"),
                }

        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Step[{self.step}] Critic parse/call failed: {e}")

        # Store critic trajectory for session JSON
        self._critic_trajectory.append({
            "step": self.step,
            "analysis": analysis,
            "has_issues": result["has_issues"],
            "issues": result["issues"],
            "revised_observation": result["revised_observation"],
        })

        return result

    # ------------------------------------------------------------------
    # Phase 3: Instruction
    # ------------------------------------------------------------------

    def _generate_instruction(self, analysis: str) -> str:
        """Given the validated analysis, ask Controller for the next instruction."""
        self.history.append({
            "role": "user",
            "content": f"Validated analysis:\n{analysis}\n\n{INSTRUCTION_NOTE}",
        })

        for _ in range(3):
            try:
                trimmed = _trim_history(self.history)
                response = get_chat_completion(trimmed, self.configs)
                if response is None:
                    logger.warning(f"Step[{self.step}] Instruction: API returned None")
                    continue

                self.history.append({"role": "assistant", "content": response})

                raw_json = _extract_json(response)
                parsed = json.loads(raw_json)

                instruction = parsed.get("instruction", "")
                completed = parsed.get("completed", "False")

                logger.info(
                    f"{'-'*70}\nStep[{self.step}] Instruction: {instruction}\n"
                    f"Completed: {completed}\n{'-'*70}"
                )

                # If completed, return submit action with the instruction as summary
                if completed == "True":
                    return self._force_submit()

                # Return instruction as execute() action for orchestrator
                # Escape newlines and quotes so ast.parse can handle it
                escaped = (instruction
                           .replace('\\', '\\\\')
                           .replace('"', '\\"')
                           .replace('\n', '\\n'))
                return f'Thought: {instruction}\n```\nexecute("{escaped}")\n```'

            except json.JSONDecodeError:
                logger.warning(f"Step[{self.step}] Instruction JSON parse failed")
                self.history.append({
                    "role": "user",
                    "content": 'Please provide your instruction in JSON format: '
                               '{"completed": "False", "instruction": "..."}',
                })
            except Exception as e:
                logger.error(f"Step[{self.step}] Instruction error: {e}")
                self.history.append({
                    "role": "user",
                    "content": f"Error: {e}. Please provide a valid JSON instruction.",
                })

        return self._force_submit()

    # ------------------------------------------------------------------
    # Force submit (matches original OpenRCA summary format)
    # ------------------------------------------------------------------

    def _force_submit(self) -> str:
        """Force a final submit when completed=True or step limit is reached."""
        self.history.append({"role": "user", "content": SUMMARY_TEMPLATE.format(
            cand=self._cand,
            objective=self.problem_desc,
        )})
        try:
            response = get_chat_completion(self.history, self.configs)
            self.history.append({"role": "assistant", "content": response})

            # Extract JSON from ```json ... ``` tags
            match = re.search(r"```json\s*(.*?)\s*```", response, re.S)
            if match:
                prediction = match.group(1).strip()
            else:
                prediction = _extract_json(response)

            return f'```\nsubmit({prediction})\n```'
        except Exception:
            return '```\nsubmit({})\n```'

    def get_model_name(self) -> str:
        """Return the controller LLM model name for save path labeling."""
        model = self.configs.get("MODEL", "unknown")
        effort = self.configs.get("REASONING_EFFORT")
        return f"{model}-{effort}" if effort else model

    def cleanup(self):
        """No-op: kernel cleanup is handled by StaticRCAActionsWithExecutor."""
        pass
