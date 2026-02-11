"""Actions for static dataset detection tasks."""

from aiopslab.orchestrator.static_actions.base import StaticTaskActions
from aiopslab.utils.actions import action
from aiopslab.utils.status import SubmissionStatus


class StaticDetectionActions(StaticTaskActions):
    """Actions for static dataset detection tasks."""

    @staticmethod
    @action
    def submit(has_anomaly: str) -> SubmissionStatus:
        """
        Submit if anomalies are detected to the orchestrator for evaluation.

        Args:
            has_anomaly (str): "Yes" if anomalies are detected, "No" otherwise.

        Returns:
            SubmissionStatus: The status of the submission.
        """
        return SubmissionStatus.VALID_SUBMISSION
