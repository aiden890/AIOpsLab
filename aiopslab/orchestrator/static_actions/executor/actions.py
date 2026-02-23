"""Self-contained executor action for OpenRCA static dataset tasks.

Extends StaticRCAActions so that execute() owns its own IPython kernel,
TelemetryHelper, executor conversation history, and LLM config.

No injected callback is needed — the runner calls setup_executor() once
and the action handles everything internally.
"""

import json
import logging
from pathlib import Path
from IPython.terminal.embed import InteractiveShellEmbed

# Minimal nbformat-compatible notebook structure (no nbformat dependency needed)
_NOTEBOOK_TEMPLATE = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {"name": "python", "version": "3"},
    },
    "cells": [],
}

from aiopslab.orchestrator.static_actions.rca import StaticRCAActions
from aiopslab.orchestrator.static_actions.executor.helper import TelemetryHelper
from aiopslab.utils.actions import executor_action
from aiopslab.orchestrator.static_actions.executor.api_router import load_config
from aiopslab.orchestrator.static_actions.executor.runner import execute_act


class StaticRCAActionsWithExecutor(StaticRCAActions):
    """StaticRCAActions where execute() is fully self-contained.

    The IPython kernel, TelemetryHelper, executor history, and LLM config
    live inside this object. Call setup_executor() once before running.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._kernel = None
        self._executor_history = []
        self._executor_trajectory = []  # [{step, instruction, code, result, success}]
        self._trajectory_path: Path | None = None
        self._notebook_path: Path | None = None
        self._background = ""
        self._configs = None
        self._logger = logging.getLogger("rca_executor")
        self._namespace = ""

    def setup_executor(
        self,
        background: str,
        api_config_path: str,
        namespace: str,
        logger=None,
        notebook_save_path: str | None = None,
    ):
        """Initialize IPython kernel, TelemetryHelper, and LLM config.

        Args:
            background: Domain schema string for the Executor LLM prompt.
            api_config_path: Path to api_config.yaml for LLM credentials.
            namespace: AIOpsLab namespace (e.g. "static-bank").
            logger: Optional logger instance.
            notebook_save_path: Path to save generated code as a .ipynb notebook.
                Each execute() call appends a new cell. Created on first call.
        """
        self._background = background
        self._configs = load_config(api_config_path)
        self._namespace = namespace
        self._executor_history = []  # persists across all execute() calls
        if logger is not None:
            self._logger = logger

        if notebook_save_path is not None:
            self._notebook_path = Path(notebook_save_path)
            self._notebook_path.parent.mkdir(parents=True, exist_ok=True)
            # Write an empty notebook to start fresh
            import copy
            nb = copy.deepcopy(_NOTEBOOK_TEMPLATE)
            self._notebook_path.write_text(json.dumps(nb, indent=1))
        else:
            self._notebook_path = None

        enabled = self.enabled_telemetry_types  # frozenset or None
        all_enabled = enabled is None

        self._kernel = InteractiveShellEmbed()
        helper = TelemetryHelper(
            actions_obj=self,
            namespace=namespace,
            enable_log=all_enabled or "log" in enabled,
            enable_metric=all_enabled or "metric" in enabled,
            enable_trace=all_enabled or "trace" in enabled,
        )
        self._kernel.push({"telemetry": helper})
        self._kernel.run_cell(
            "import pandas as pd\n"
            "pd.set_option('display.width', 427)\n"
            "pd.set_option('display.max_columns', 10)\n"
        )

    @executor_action
    def execute(self, instruction: str) -> str:
        """Generate and run Python code for custom telemetry analysis.

        The Executor LLM writes Python code from your instruction, runs it
        in a stateful IPython kernel, and returns a summarized result.
        Variables persist across calls — reuse them to avoid redundant fetches.
        Max 3 retries on execution error.

        Args:
            instruction: Detailed natural language description of what to analyze.
        """
        if self._kernel is None:
            return "Error: Executor not initialized. Call setup_executor() first."

        code, result, success, self._executor_history = execute_act(
            instruction=instruction,
            background=self._background,
            history=self._executor_history,
            kernel=self._kernel,
            configs=self._configs,
            logger=self._logger,
            max_retries=3,
        )

        if not success:
            self._logger.warning("Executor self-correction exhausted all retries.")

        step = len(self._executor_trajectory) + 1
        self._executor_trajectory.append({
            "step": step,
            "instruction": instruction,
            "code": code,
            "result": result,
            "success": success,
        })

        if self._notebook_path is not None:
            self._append_notebook_cell(step, instruction, code, result)

        return result

    def _append_notebook_cell(self, step: int, instruction: str, code: str, result: str):
        """Append a markdown header + code cell to the .ipynb notebook file."""
        try:
            nb = json.loads(self._notebook_path.read_text())

            # Markdown cell: step header and instruction
            nb["cells"].append({
                "cell_type": "markdown",
                "metadata": {},
                "source": f"## Step {step}\n\n**Instruction:** {instruction}",
            })

            # Code cell: generated Python code
            nb["cells"].append({
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": result,
                    }
                ],
                "source": code,
            })

            self._notebook_path.write_text(json.dumps(nb, indent=1))
        except Exception as exc:
            self._logger.warning(f"Failed to save notebook cell: {exc}")

    def cleanup(self):
        """Release IPython kernel resources."""
        if self._kernel is not None:
            self._kernel.reset()
            self._kernel = None
