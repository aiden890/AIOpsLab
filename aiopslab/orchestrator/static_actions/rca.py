"""Unified actions for OpenRCA static dataset tasks.

All 7 task types use the same submit format (JSON dict).
"""

from aiopslab.orchestrator.static_actions.base import StaticTaskActions
from aiopslab.utils.actions import action
from aiopslab.utils.status import SubmissionStatus


class StaticRCAActions(StaticTaskActions):
    """Actions for OpenRCA root cause analysis tasks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._executor_fn = None

    def set_executor(self, executor_fn):
        """Inject an executor callback from the RCA agent.

        Args:
            executor_fn: Callable(instruction: str) -> str
        """
        self._executor_fn = executor_fn

    @action
    def execute(self, instruction: str) -> str:
        """Execute a natural language instruction via the RCA Executor.

        The Executor generates Python code, runs it in an IPython kernel,
        and returns a summarized result.

        Args:
            instruction (str): Natural language instruction for data analysis.

        Returns:
            str: Summarized analysis result from the Executor.
        """
        if self._executor_fn is None:
            return "Error: Executor not initialized. Call set_executor() first."
        return self._executor_fn(instruction)

    @staticmethod
    @action
    def submit(prediction: dict) -> SubmissionStatus:
        """
        Submit root cause analysis prediction.

        Args:
            prediction (dict): JSON dict with numbered keys ("1", "2", ...).
                Each value is a dict with optional fields:
                - "root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS"
                - "root cause component": "component_name"
                - "root cause reason": "fault_reason"

        Returns:
            SubmissionStatus: The status of the submission.
        """
        return SubmissionStatus.VALID_SUBMISSION
