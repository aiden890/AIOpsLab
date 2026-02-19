"""Unified actions for OpenRCA static dataset tasks.

All 7 task types use the same submit format (JSON dict).
"""

from aiopslab.orchestrator.static_actions.base import StaticTaskActions
from aiopslab.utils.actions import action
from aiopslab.utils.status import SubmissionStatus


class StaticRCAActions(StaticTaskActions):
    """Actions for OpenRCA root cause analysis tasks."""

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
