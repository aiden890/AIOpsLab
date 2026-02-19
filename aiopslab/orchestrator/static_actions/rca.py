"""Unified actions for OpenRCA static dataset tasks.

All 7 task types use the same submit format (JSON dict).
"""

from aiopslab.orchestrator.static_actions.base import StaticTaskActions
from aiopslab.utils.actions import action
from aiopslab.utils.status import SubmissionStatus


class StaticRCAActions(StaticTaskActions):
    """Actions for OpenRCA root cause analysis tasks."""

    def __init__(self, *args, possible_root_causes=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._executor_fn = None
        prc = possible_root_causes or {}
        self.possible_components = prc.get("components", [])
        self.possible_reasons = prc.get("reasons", [])

    def set_executor(self, executor_fn):
        """Inject an executor callback from the RCA agent.

        Args:
            executor_fn: Callable(instruction: str) -> str
        """
        self._executor_fn = executor_fn

    @action
    def execute(self, instruction: str) -> str:
        """Runs Python code in an IPython kernel. Use for pandas data analysis on fetched CSVs."""
        if self._executor_fn is None:
            return "Error: Executor not initialized. Call set_executor() first."
        return self._executor_fn(instruction)

    @action
    def submit(self, prediction: dict):
        """
        Submit root cause analysis prediction.

        Args:
            prediction (dict): JSON dict with numbered keys ("1", "2", ...).
                Each value is a dict with optional fields:
                - "root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS"
                - "root cause component": "component_name"
                - "root cause reason": "fault_reason"

        Returns:
            SubmissionStatus or str: VALID_SUBMISSION if accepted, error message if invalid.
        """
        errors = []
        for key, entry in prediction.items():
            if not isinstance(entry, dict):
                continue
            component = entry.get("root cause component", "")
            reason = entry.get("root cause reason", "")

            if self.possible_components and component and component not in self.possible_components:
                errors.append(
                    f"  [{key}] Invalid 'root cause component': '{component}'.\n"
                    f"       Must be one of: {self.possible_components}"
                )
            if self.possible_reasons and reason and reason not in self.possible_reasons:
                errors.append(
                    f"  [{key}] Invalid 'root cause reason': '{reason}'.\n"
                    f"       Must be one of: {self.possible_reasons}"
                )

        if errors:
            return (
                "Submission rejected - invalid values:\n"
                + "\n".join(errors)
                + "\n\nPlease correct and resubmit."
            )

        return SubmissionStatus.VALID_SUBMISSION
