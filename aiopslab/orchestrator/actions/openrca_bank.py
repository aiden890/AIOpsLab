# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Actions for OpenRCA Bank domain."""

from aiopslab.utils.actions import action, read
from aiopslab.utils.status import SubmissionStatus
from aiopslab.observer.openrca_bank import BankDataset


class OpenrcaBankActions:
    """Actions for OpenRCA Bank RCA task.

    Provides telemetry data access APIs for the agent.
    Does NOT inherit from TaskActions to avoid K8s/Prometheus dependencies.
    """

    def __init__(self, dataset: BankDataset):
        """Initialize with a BankDataset instance.

        Args:
            dataset: BankDataset instance for data access
        """
        self.dataset = dataset

    @read
    def get_metric_container(
        self,
        start_time: str = None,
        end_time: str = None
    ) -> str:
        """
        Get container metrics from the Bank system.

        Schema: timestamp, cmdb_id, kpi_name, value

        Args:
            start_time (str): Start time for filtering (e.g., "2021-03-04 14:30:00"). Optional.
            end_time (str): End time for filtering (e.g., "2021-03-04 15:00:00"). Optional.

        Returns:
            str: Container metrics as CSV string (limited to first 100 rows for display).
        """
        df = self.dataset.get_metric_container(start_time=start_time, end_time=end_time)
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_metric_app(
        self,
        start_time: str = None,
        end_time: str = None
    ) -> str:
        """
        Get application metrics from the Bank system.

        Schema: timestamp, rr (response rate), sr (success rate), cnt (count), mrt (mean response time), tc (service name)

        Args:
            start_time (str): Start time for filtering (e.g., "2021-03-04 14:30:00"). Optional.
            end_time (str): End time for filtering (e.g., "2021-03-04 15:00:00"). Optional.

        Returns:
            str: Application metrics as CSV string (limited to first 100 rows for display).
        """
        df = self.dataset.get_metric_app(start_time=start_time, end_time=end_time)
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_traces(
        self,
        start_time: str = None,
        end_time: str = None
    ) -> str:
        """
        Get distributed traces from the Bank system.

        Schema: timestamp, cmdb_id, parent_id, span_id, trace_id, duration

        Args:
            start_time (str): Start time for filtering (e.g., "2021-03-04 14:30:00"). Optional.
            end_time (str): End time for filtering (e.g., "2021-03-04 15:00:00"). Optional.

        Returns:
            str: Trace spans as CSV string (limited to first 100 rows for display).
        """
        df = self.dataset.get_traces(start_time=start_time, end_time=end_time)
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_logs(
        self,
        start_time: str = None,
        end_time: str = None
    ) -> str:
        """
        Get service logs from the Bank system.

        Schema: log_id, timestamp, cmdb_id, log_name, value

        Args:
            start_time (str): Start time for filtering (e.g., "2021-03-04 14:30:00"). Optional.
            end_time (str): End time for filtering (e.g., "2021-03-04 15:00:00"). Optional.

        Returns:
            str: Logs as CSV string (limited to first 100 rows for display).
        """
        df = self.dataset.get_logs(start_time=start_time, end_time=end_time)
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @staticmethod
    @action
    def submit(solution: dict) -> SubmissionStatus:
        """
        Submit the root cause analysis solution for evaluation.

        Args:
            solution (dict): A dictionary containing the RCA result with keys:
                - "root cause occurrence datetime": str (e.g., "2021-03-04 14:57:00")
                - "root cause component": str (e.g., "Redis02")
                - "root cause reason": str (e.g., "high memory usage")

                Note: Depending on the task type, not all fields may be required.
                - task_1: only "root cause occurrence datetime"
                - task_2: only "root cause reason"
                - task_3: only "root cause component"
                - task_4: "root cause occurrence datetime" + "root cause reason"
                - task_5: "root cause occurrence datetime" + "root cause component"
                - task_6: "root cause component" + "root cause reason"
                - task_7: all three fields

        Returns:
            SubmissionStatus: The status of the submission.
        """
        return SubmissionStatus.VALID_SUBMISSION
