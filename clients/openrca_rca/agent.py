"""OpenRCA RCA Agent for AIOpsLab.

Two-agent architecture (Controller + Executor) integrated with the
orchestrator's start_problem() loop:

- get_action() = Controller LLM step
  → returns execute("instruction") or submit({answer})
- execute action = Executor (code gen + IPython + summary)
  → runs via perform_action("execute", instruction)

The orchestrator loop drives the Controller loop.
"""

import json
import re
import logging
from pathlib import Path

from IPython.terminal.embed import InteractiveShellEmbed

from clients.openrca_rca.api_router import load_config, get_chat_completion
from clients.openrca_rca.executor import execute_act
from clients.openrca_rca.telemetry_helper import TelemetryHelper
from clients.openrca_rca.prompts import get_basic_prompt, controller_rules

logger = logging.getLogger("openrca_rca")

# --- Controller prompt templates ---

SYSTEM_TEMPLATE = """You are the Administrator of a DevOps Assistant system for failure diagnosis. To solve each given issue, you should iteratively instruct an Executor to write and execute Python code for data analysis on telemetry files of target system. By analyzing the execution results, you should approximate the answer step-by-step.

There is some domain knowledge for you:

{background}

{agent}

The issue you are going to solve is:

{objective}

Solve the issue step-by-step. In each step, your response should follow the JSON format below:

{format}

Let's begin."""

RESPONSE_FORMAT = """{
    "analysis": (Your analysis of the code execution result from Executor in the last step, with detailed reasoning of 'what have been done' and 'what can be derived'. Respond 'None' if it is the first step.),
    "completed": ("True" if you believe the issue is resolved, and an answer can be derived in the 'instruction' field. Otherwise "False"),
    "instruction": (Your instruction for the Executor to perform via code execution in the next step. Do not involve complex multi-step instruction. Keep your instruction atomic, with clear request of 'what to do' and 'how to do'. Respond a summary by yourself if you believe the issue is resolved.)
}
(DO NOT contain "```json" and "```" tags. DO contain the JSON object with the brackets "{}" only. Use '\\n' instead of an actual newline character to ensure JSON compatibility when you want to insert a line break within a string.)"""

SUMMARY_TEMPLATE = """Now, you have decided to finish your reasoning process. You should now provide the final answer to the issue. The candidates of possible root cause components and reasons are provided to you. The root cause components and reasons must be selected from the provided candidates.

{cand}

Recall the issue is: {objective}

Please first review your previous reasoning process to infer an exact answer of the issue. Then, summarize your final answer of the root causes using the following JSON format at the end of your response:

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
(Please use "```json" and "```" tags to wrap the JSON object. You only need to provide the elements asked by the issue, and ommited the other fields in the JSON.)
Note that all the root cause components and reasons must be selected from the provided candidates. Do not reply 'unknown' or 'null' or 'not found' in the JSON. Do not be too conservative in selecting the root cause components and reasons. Be decisive to infer a possible answer based on your current observation."""


class OpenRCARCAAgent:
    """OpenRCA RCA Agent integrated with AIOpsLab orchestrator loop.

    - get_action() acts as the Controller (1 LLM call per orchestrator step)
    - Executor runs via the `execute` action on StaticRCAActions
    """

    def __init__(self, api_config_path=None):
        if api_config_path is None:
            api_config_path = str(Path(__file__).parent / "api_config.yaml")
        self.configs = load_config(api_config_path)

    def init_context(self, problem_desc, instructions, apis):
        """AIOpsLab interface: receive problem context and initialize Controller state."""
        self.problem_desc = problem_desc
        self.instructions = instructions
        self.apis = apis

        # Controller conversation history (built once, persists across steps)
        self.controller_prompt = []
        self.step = 0

    def set_actions(self, actions_obj, namespace, dataset_key, max_steps=25):
        """Initialize Executor and inject it into the actions object.

        Args:
            actions_obj: StaticRCAActions instance (has set_executor method).
            namespace: AIOpsLab namespace (e.g., "static-bank").
            dataset_key: Dataset identifier (e.g., "openrca_bank").
            max_steps: Max orchestrator steps (forces submit on last step).
        """
        self.namespace = namespace
        self.dataset_key = dataset_key
        self.max_steps = max_steps
        self.orchestrator_step = 0
        self.basic_prompt = get_basic_prompt(dataset_key)

        # Initialize IPython kernel with telemetry helper
        self.kernel = InteractiveShellEmbed()
        helper = TelemetryHelper(actions_obj, namespace)
        self.kernel.push({"telemetry": helper})
        self.kernel.run_cell(
            "import pandas as pd\n"
            "pd.set_option('display.width', 427)\n"
            "pd.set_option('display.max_columns', 10)\n"
        )
        self.executor_history = []
        self.executor_trajectory = []

        # Build Controller system prompt
        self.controller_prompt = [
            {"role": "system", "content": SYSTEM_TEMPLATE.format(
                objective=self.problem_desc,
                format=RESPONSE_FORMAT,
                agent=controller_rules,
                background=self.basic_prompt.schema,
            )},
            {"role": "user", "content": "Let's begin."},
        ]

        # Inject Executor callback into actions
        actions_obj.set_executor(self._run_executor)

    def _run_executor(self, instruction):
        """Executor callback: code gen → IPython execution → LLM summary.

        Called by perform_action("execute", instruction).
        """
        code, result, status, self.executor_history = execute_act(
            instruction,
            self.basic_prompt.schema,
            self.executor_history,
            self.kernel,
            self.configs,
            logger,
        )

        self.step += 1
        if not status:
            logger.warning(f"Step[{self.step}] Executor self-correction failed.")

        # Record executor trajectory
        self.executor_trajectory.append({
            "step": self.step,
            "instruction": instruction,
            "code": code,
            "result": result,
            "success": status,
        })

        logger.info(f"{'-' * 80}\nStep[{self.step}] Observation:\n{result}\n{'-' * 80}")
        return result

    async def get_action(self, feedback):
        """Controller LLM step. Called by orchestrator's ask_agent().

        Args:
            feedback: Environment response from previous step (Executor result or init).

        Returns:
            str: Action string - execute("instruction") or submit({answer}).
        """
        # Add Executor result as feedback to Controller history
        # (skip adding on first call - "Let's begin." is already in prompt)
        if self.step > 0:
            self.controller_prompt.append({"role": "user", "content": feedback})

        self.orchestrator_step += 1

        # Force submit on last orchestrator step (like original OpenRCA's forced answer)
        if self.orchestrator_step >= self.max_steps:
            logger.info("Max orchestrator steps reached, forcing final submit.")
            return self._generate_submit()

        note = [{"role": "user", "content": (
            f"Continue your reasoning process for the target issue described above.\n\n"
            f"Follow the rules during issue solving:\n\n{controller_rules}.\n\n"
            f"Response format:\n\n{RESPONSE_FORMAT}"
        )}]

        try:
            response_raw = get_chat_completion(
                self.controller_prompt + note, self.configs
            )

            # Strip ```json wrapper if present
            if "```json" in response_raw:
                match = re.search(r"```json\n(.*)\n```", response_raw, re.S)
                if match:
                    response_raw = match.group(1).strip()

            logger.debug(f"Controller raw response:\n{response_raw}")

            # Validate JSON fields
            if ('"analysis":' not in response_raw or
                    '"instruction":' not in response_raw or
                    '"completed":' not in response_raw):
                logger.warning("Invalid Controller response format.")
                self.controller_prompt.append({"role": "assistant", "content": response_raw})
                self.controller_prompt.append({"role": "user", "content":
                    "Please provide your analysis in requested JSON format."})
                # Return execute with a retry instruction
                return '```\nexecute("Please retry the previous instruction.")\n```'

            response = json.loads(response_raw)
            analysis = response["analysis"]
            instruction = response["instruction"]
            completed = response["completed"]

            logger.info(
                f"{'-' * 80}\n### Controller Step[{self.step + 1}]\n"
                f"Analysis: {analysis}\nInstruction: {instruction}\n"
                f"Completed: {completed}\n{'-' * 80}"
            )

            self.controller_prompt.append({"role": "assistant", "content": response_raw})

            # --- Completed: generate final answer and submit ---
            if completed == "True":
                return self._generate_submit()

            # --- Not completed: return execute action ---
            # Escape quotes in instruction for parser compatibility
            escaped = instruction.replace('"', '\\"')
            return f'```\nexecute("{escaped}")\n```'

        except Exception as e:
            logger.error(f"Controller error: {e}")
            if "context_length_exceeded" in str(e):
                return self._generate_submit()
            # On error, ask Executor to retry
            return '```\nexecute("Please retry the previous instruction.")\n```'

    def cleanup(self):
        """Clean up resources (IPython kernel)."""
        if hasattr(self, "kernel"):
            self.kernel.reset()

    def _generate_submit(self):
        """Generate final answer via summary LLM call and return submit action."""
        self.controller_prompt.append({"role": "user", "content": SUMMARY_TEMPLATE.format(
            objective=self.problem_desc, cand=self.basic_prompt.cand
        )})

        try:
            answer = get_chat_completion(self.controller_prompt, self.configs)
            logger.debug(f"Final answer raw:\n{answer}")
            self.controller_prompt.append({"role": "assistant", "content": answer})

            # Extract JSON from answer
            if "```json" in answer:
                match = re.search(r"```json\s*(.*?)\s*```", answer, re.S)
                if match:
                    answer = match.group(1).strip()

            self.kernel.reset()
            return f'```\nsubmit({answer})\n```'

        except Exception as e:
            logger.error(f"Summary generation error: {e}")
            self.kernel.reset()
            return '```\nsubmit({})\n```'
