# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Actions for AcmeTrace Kalos GPU Cluster RCA tasks."""

from typing import List, Optional

from aiopslab.utils.actions import action, read
from aiopslab.utils.status import SubmissionStatus
from aiopslab.observer.acme_kalos import KalosDataset


class KalosActions:
    """Actions for AcmeTrace Kalos GPU Cluster RCA task.

    Provides telemetry data access APIs for the agent.
    Does NOT inherit from TaskActions to avoid K8s/Prometheus dependencies.
    """

    def __init__(self, dataset: KalosDataset):
        """Initialize with a KalosDataset instance.

        Args:
            dataset: KalosDataset instance for data access
        """
        self.dataset = dataset

    @read
    def get_jobs(
        self,
        start_time: str = None,
        end_time: str = None,
        state: str = None,
        gpu_only: bool = True
    ) -> str:
        """
        Get job trace data from the Kalos GPU cluster.

        Schema: job_id, user, node_num, gpu_num, cpu_num, mem_per_pod_GB,
                state, submit_time, start_time, end_time, fail_time, duration, gpu_time

        Args:
            start_time (str): Start time for filtering (e.g., "2023-08-15"). Optional.
            end_time (str): End time for filtering (e.g., "2023-08-16"). Optional.
            state (str): Filter by job state (COMPLETED, FAILED, TIMEOUT, NODE_FAIL). Optional.
            gpu_only (bool): If True, only return GPU jobs. Default True.

        Returns:
            str: Job traces as string (limited to first 100 rows for display).
        """
        df = self.dataset.get_jobs(
            start_time=start_time,
            end_time=end_time,
            state=state,
            gpu_only=gpu_only
        )
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} jobs:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_failed_jobs(
        self,
        start_time: str = None,
        end_time: str = None
    ) -> str:
        """
        Get failed GPU jobs from the Kalos cluster.

        Returns jobs with state in [FAILED, TIMEOUT, NODE_FAIL].

        Args:
            start_time (str): Start time for filtering. Optional.
            end_time (str): End time for filtering. Optional.

        Returns:
            str: Failed jobs as string (limited to first 100 rows).
        """
        df = self.dataset.get_failed_jobs(start_time=start_time, end_time=end_time)
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} failed jobs:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_gpu_util(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> str:
        """
        Get GPU utilization metrics (0-100%).

        Schema: Time, {IP}-{GPU_ID} columns (e.g., 172.31.15.112-6)

        Args:
            start_time (str): Start time for filtering. Optional.
            end_time (str): End time for filtering. Optional.
            gpus (List[str]): List of GPU IDs to filter (e.g., ["172.31.15.112-6"]). Optional.

        Returns:
            str: GPU utilization as string (limited to first 100 rows).
        """
        df = self.dataset.get_gpu_util(start_time=start_time, end_time=end_time, gpus=gpus)
        if df.empty:
            return "No GPU utilization data found for the specified time range."
        gpu_cols = [c for c in df.columns if c != "Time"]
        if not gpus and len(gpu_cols) > 20:
            return (f"Too many GPUs ({len(gpu_cols)}) to display. "
                    f"Please specify specific GPUs using the 'gpus' parameter.\n"
                    f"Available GPUs (first 30): {', '.join(gpu_cols[:30])}...")
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_gpu_temp(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> str:
        """
        Get GPU temperature metrics (Celsius).

        Schema: Time, {IP}-{GPU_ID} columns

        Args:
            start_time (str): Start time for filtering. Optional.
            end_time (str): End time for filtering. Optional.
            gpus (List[str]): List of GPU IDs to filter. Optional.

        Returns:
            str: GPU temperature as string (limited to first 100 rows).
        """
        df = self.dataset.get_gpu_temp(start_time=start_time, end_time=end_time, gpus=gpus)
        if df.empty:
            return "No GPU temperature data found for the specified time range."
        gpu_cols = [c for c in df.columns if c != "Time"]
        if not gpus and len(gpu_cols) > 20:
            return (f"Too many GPUs ({len(gpu_cols)}) to display. "
                    f"Please specify specific GPUs using the 'gpus' parameter.\n"
                    f"Available GPUs (first 30): {', '.join(gpu_cols[:30])}...")
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_gpu_memory(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> str:
        """
        Get GPU frame buffer memory used (MB).

        Schema: Time, {IP}-{GPU_ID} columns

        Args:
            start_time (str): Start time for filtering. Optional.
            end_time (str): End time for filtering. Optional.
            gpus (List[str]): List of GPU IDs to filter. Optional.

        Returns:
            str: GPU memory usage as string (limited to first 100 rows).
        """
        df = self.dataset.get_gpu_memory(start_time=start_time, end_time=end_time, gpus=gpus)
        if df.empty:
            return "No GPU memory data found for the specified time range."
        gpu_cols = [c for c in df.columns if c != "Time"]
        if not gpus and len(gpu_cols) > 20:
            return (f"Too many GPUs ({len(gpu_cols)}) to display. "
                    f"Please specify specific GPUs using the 'gpus' parameter.\n"
                    f"Available GPUs (first 30): {', '.join(gpu_cols[:30])}...")
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_power_usage(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> str:
        """
        Get GPU power consumption (Watts).

        Schema: Time, {IP}-{GPU_ID} columns

        Args:
            start_time (str): Start time for filtering. Optional.
            end_time (str): End time for filtering. Optional.
            gpus (List[str]): List of GPU IDs to filter. Optional.

        Returns:
            str: GPU power usage as string (limited to first 100 rows).
        """
        df = self.dataset.get_power_usage(start_time=start_time, end_time=end_time, gpus=gpus)
        if df.empty:
            return "No power usage data found for the specified time range."
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_xid_errors(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> str:
        """
        Get XID error codes from NVIDIA GPUs.

        XID codes indicate GPU hardware/driver errors:
        - 31: ECC Error (memory page retirement)
        - 43: NVLink Error (GPU has fallen off the bus)
        - 45: CUDA Error (preemptive cleanup)

        Schema: Time, {IP}-{GPU_ID} columns (value is XID code, 0 = no error)

        Args:
            start_time (str): Start time for filtering. Optional.
            end_time (str): End time for filtering. Optional.
            gpus (List[str]): List of GPU IDs to filter. Optional.

        Returns:
            str: XID errors as string (limited to first 100 rows).
        """
        df = self.dataset.get_xid_errors(start_time=start_time, end_time=end_time, gpus=gpus)
        if df.empty:
            return "No XID error data found for the specified time range."
        # Only show GPUs that have non-zero XID errors
        xid_cols = [c for c in df.columns if c != "Time"]
        active_cols = [c for c in xid_cols if (df[c].fillna(0) != 0).any()]
        if not active_cols:
            return "No non-zero XID errors found in the specified time range."
        df = df[["Time"] + active_cols]
        # Only show rows with at least one non-zero XID
        row_mask = (df[active_cols].fillna(0) != 0).any(axis=1)
        df = df[row_mask]
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows ({len(active_cols)} GPUs with errors):\n" + df.head(100).to_string(index=False)
        return f"{len(active_cols)} GPUs with XID errors:\n" + df.to_string(index=False)

    @read
    def get_node_cpu(
        self,
        start_time: str = None,
        end_time: str = None,
        nodes: List[str] = None
    ) -> str:
        """
        Get node CPU utilization (0-100%).

        Schema: Time, {IP} columns (node IP addresses)

        Args:
            start_time (str): Start time for filtering. Optional.
            end_time (str): End time for filtering. Optional.
            nodes (List[str]): List of node IPs to filter. Optional.

        Returns:
            str: Node CPU utilization as string (limited to first 100 rows).
        """
        df = self.dataset.get_node_cpu(start_time=start_time, end_time=end_time, nodes=nodes)
        if df.empty:
            return "No node CPU data found for the specified time range."
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_node_memory(
        self,
        start_time: str = None,
        end_time: str = None,
        nodes: List[str] = None
    ) -> str:
        """
        Get node memory utilization (0-100%).

        Schema: Time, {IP} columns (node IP addresses)

        Args:
            start_time (str): Start time for filtering. Optional.
            end_time (str): End time for filtering. Optional.
            nodes (List[str]): List of node IPs to filter. Optional.

        Returns:
            str: Node memory utilization as string (limited to first 100 rows).
        """
        df = self.dataset.get_node_memory(start_time=start_time, end_time=end_time, nodes=nodes)
        if df.empty:
            return "No node memory data found for the specified time range."
        if len(df) > 100:
            return f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return df.to_string(index=False)

    @read
    def get_xid_errors_for_job(
        self,
        job_id: str,
        buffer_minutes: int = 1
    ) -> str:
        """
        Find XID errors that occurred during a specific job's execution.

        Args:
            job_id (str): Job identifier to look up.
            buffer_minutes (int): Time buffer before/after job window. Default 1.

        Returns:
            str: XID errors during job execution.
        """
        df = self.dataset.find_xid_errors_for_job(job_id, buffer_minutes)
        if df.empty:
            return f"No XID errors found during job {job_id} execution."
        gpu_cols = [c for c in df.columns if c != "Time"]
        summary = f"XID errors found for job {job_id} on {len(gpu_cols)} GPU(s): {', '.join(gpu_cols)}\n"
        if len(df) > 100:
            return summary + f"Showing first 100 of {len(df)} rows:\n" + df.head(100).to_string(index=False)
        return summary + df.to_string(index=False)

    @read
    def get_cluster_stats(self) -> str:
        """
        Get cluster statistics summary.

        Returns:
            str: Summary of cluster statistics (total jobs, GPUs, failure rate, etc.)
        """
        stats = self.dataset.get_stats()
        lines = [
            "Cluster Statistics:",
            f"  Total jobs: {stats['total_jobs']}",
            f"  GPU jobs: {stats['gpu_jobs']}",
            f"  Failed jobs: {stats['failed_jobs']}",
            f"  Failure rate: {stats['failure_rate']:.2%}",
            f"  GPUs: {stats['gpus']}",
            f"  Nodes: {stats['nodes']}",
        ]
        if 'categories' in stats:
            lines.append("  Categories:")
            for cat, count in stats['categories'].items():
                if cat:  # Skip None
                    lines.append(f"    {cat}: {count}")
        return "\n".join(lines)

    @staticmethod
    @action
    def submit(solution: dict) -> SubmissionStatus:
        """
        Submit the root cause analysis solution for evaluation.

        Args:
            solution (dict): A dictionary containing the RCA result with keys:
                - "job_id": str - The job ID being analyzed
                - "category": str - Error category (Infrastructure, Framework, Script)
                - "reason": str - Specific error reason (e.g., "NVLink Error", "Out of Memory")
                - "affected_node": str - Node IP if identified (optional)
                - "affected_gpu": str - GPU ID if identified (optional)

                For different task types:
                - detection: only requires "is_failure" (bool)
                - localization: requires "affected_node" and/or "affected_gpu"
                - analysis: requires "category" and "reason"

        Returns:
            SubmissionStatus: The status of the submission.
        """
        return SubmissionStatus.VALID_SUBMISSION
