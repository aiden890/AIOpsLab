# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Kalos GPU Cluster Dataset loader for AcmeTrace RCA tasks."""

from pathlib import Path
from datetime import datetime
from typing import Optional, Union, List

import pandas as pd

from aiopslab.paths import ACME_KALOS_DIR


class KalosDataset:
    """Dataset loader for AcmeTrace Kalos GPU cluster.

    Provides access to:
    - Job traces
    - GPU/Node utilization metrics
    - XID error codes
    - Ground truth labels (for sampled data)
    - Evaluation queries (for sampled data)

    All telemetry methods support optional time filtering.

    Reference:
    - Paper: AcmeTrace (NSDI'24)
    - Dataset: https://huggingface.co/datasets/Qinghao/AcmeTrace
    """

    # Available utilization files
    UTIL_FILES = [
        "GPU_UTIL.csv",
        "GPU_TEMP.csv",
        "XID_ERRORS.csv",
        "FB_USED.csv",
        "FB_FREE.csv",
        "POWER_USAGE.csv",
        "SM_ACTIVE.csv",
        "MEM_CLOCK.csv",
        "MEM_COPY_UTIL.csv",
        "MEMORY_TEMP.csv",
        "PIPE_TENSOR_ACTIVE.csv",
        "DRAM_ACTIVE.csv",
        "NODE_CPU_UTILIZATION.csv",
        "NODE_MEMORY_UTILIZATION.csv",
    ]
    MODE = "test" # "test" or "prod"

    def __init__(self, sample_dir: str = None):
        """Initialize KalosDataset.

        Args:
            sample_dir: Path to sampled dataset (e.g., "samples/kalos_rca").
                       If None, uses raw AcmeTrace data.
        """
        if self.MODE == "test":
            self.base_path = ACME_KALOS_DIR / "samples" / "kalos_rca"
        else:
            self.base_path = ACME_KALOS_DIR / "AcmeTrace" / "data"
        self.sample_dir = sample_dir
        self._cached_data = {}

    def _get_data_path(self) -> Path:
        """Get data path (sampled or raw)."""
        return self.base_path

    def _parse_timestamp(self, ts: Union[str, int, float, datetime]) -> datetime:
        """Parse timestamp to datetime.

        Args:
            ts: Timestamp as string, int (epoch), float, or datetime

        Returns:
            datetime object (timezone-aware if possible)
        """
        if isinstance(ts, datetime):
            return ts
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts)
        if isinstance(ts, str):
            # Try common formats
            for fmt in [
                "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S+00:00",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
            ]:
                try:
                    return datetime.strptime(ts, fmt)
                except ValueError:
                    continue
            # Try pandas parsing as fallback
            return pd.to_datetime(ts).to_pydatetime()
        raise TypeError(f"Unsupported timestamp type: {type(ts)}")

    def _filter_by_time(
        self,
        df: pd.DataFrame,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timestamp_col: str = "Time"
    ) -> pd.DataFrame:
        """Filter DataFrame by time range.

        Args:
            df: DataFrame to filter
            start_time: Start time (inclusive)
            end_time: End time (inclusive)
            timestamp_col: Name of timestamp column

        Returns:
            Filtered DataFrame
        """
        if start_time is None and end_time is None:
            return df

        if timestamp_col not in df.columns:
            return df

        df = df.copy()

        # Convert timestamp column to datetime with UTC
        df["_ts"] = pd.to_datetime(df[timestamp_col], utc=True)

        if start_time is not None:
            start_dt = pd.to_datetime(start_time, utc=True)
            df = df[df["_ts"] >= start_dt]

        if end_time is not None:
            end_dt = pd.to_datetime(end_time, utc=True)
            df = df[df["_ts"] <= end_dt]

        return df.drop(columns=["_ts"])

    # =========================================================================
    # Job Trace
    # =========================================================================

    def get_jobs(
        self,
        start_time: str = None,
        end_time: str = None,
        state: str = None,
        gpu_only: bool = False
    ) -> pd.DataFrame:
        """Get job trace data.

        Schema: job_id, user, node_num, gpu_num, cpu_num, mem_per_pod_GB,
                shared_mem_per_pod, type, state, submit_time, start_time,
                end_time, fail_time, stop_time, duration, queue, gpu_time

        Args:
            start_time: Filter by job start time (optional)
            end_time: Filter by job start time (optional)
            state: Filter by job state (COMPLETED, FAILED, etc.)
            gpu_only: If True, only return GPU jobs

        Returns:
            DataFrame with job traces
        """
        data_path = self._get_data_path()
        path = data_path / "job_trace" / "trace_kalos.csv"
        df = pd.read_csv(path)

        # Filter by state
        if state:
            df = df[df["state"] == state]

        # Filter GPU jobs
        if gpu_only:
            df = df[df["gpu_num"] > 0]

        # Filter by time
        if start_time or end_time:
            df = df.copy()
            df["_start_dt"] = pd.to_datetime(df["start_time"], utc=True)
            if start_time:
                df = df[df["_start_dt"] >= pd.to_datetime(start_time, utc=True)]
            if end_time:
                df = df[df["_start_dt"] <= pd.to_datetime(end_time, utc=True)]
            df = df.drop(columns=["_start_dt"])

        return df

    def get_failed_jobs(
        self,
        start_time: str = None,
        end_time: str = None
    ) -> pd.DataFrame:
        """Get failed GPU jobs.

        Args:
            start_time: Filter by job start time (optional)
            end_time: Filter by job start time (optional)

        Returns:
            DataFrame with failed jobs
        """
        df = self.get_jobs(start_time, end_time, gpu_only=True)
        return df[df["state"].isin(["FAILED", "TIMEOUT", "NODE_FAIL"])]

    # =========================================================================
    # Utilization Metrics
    # =========================================================================

    def get_utilization(
        self,
        metric: str,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> pd.DataFrame:
        """Get utilization metric data.

        Args:
            metric: Metric file name (e.g., "GPU_UTIL.csv" or "GPU_UTIL")
            start_time: Filter start time (optional)
            end_time: Filter end time (optional)
            gpus: List of GPU IDs to filter (e.g., ["172.31.15.112-6"])

        Returns:
            DataFrame with utilization data
        """
        if not metric.endswith(".csv"):
            metric = metric + ".csv"

        data_path = self._get_data_path()
        if self.sample_dir:
            path = data_path / "utilization" / metric
        else:
            path = data_path / "utilization" / "kalos" / metric

        if not path.exists():
            return pd.DataFrame()

        df = pd.read_csv(path)

        # Filter by time
        df = self._filter_by_time(df, start_time, end_time, "Time")

        # Filter by GPUs
        if gpus:
            cols = ["Time"] + [c for c in gpus if c in df.columns]
            df = df[cols]

        return df

    def get_gpu_util(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> pd.DataFrame:
        """Get GPU utilization (%)."""
        return self.get_utilization("GPU_UTIL", start_time, end_time, gpus)

    def get_gpu_temp(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> pd.DataFrame:
        """Get GPU temperature (Celsius)."""
        return self.get_utilization("GPU_TEMP", start_time, end_time, gpus)

    def get_gpu_memory(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> pd.DataFrame:
        """Get GPU frame buffer used (MB)."""
        return self.get_utilization("FB_USED", start_time, end_time, gpus)

    def get_power_usage(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> pd.DataFrame:
        """Get GPU power consumption (W)."""
        return self.get_utilization("POWER_USAGE", start_time, end_time, gpus)

    def get_node_cpu(
        self,
        start_time: str = None,
        end_time: str = None,
        nodes: List[str] = None
    ) -> pd.DataFrame:
        """Get node CPU utilization (%)."""
        return self.get_utilization("NODE_CPU_UTILIZATION", start_time, end_time, nodes)

    def get_node_memory(
        self,
        start_time: str = None,
        end_time: str = None,
        nodes: List[str] = None
    ) -> pd.DataFrame:
        """Get node memory utilization (%)."""
        return self.get_utilization("NODE_MEMORY_UTILIZATION", start_time, end_time, nodes)

    # =========================================================================
    # XID Errors
    # =========================================================================

    def get_xid_errors(
        self,
        start_time: str = None,
        end_time: str = None,
        gpus: List[str] = None
    ) -> pd.DataFrame:
        """Get XID error codes.

        XID codes: 31 (ECC Error), 43 (NVLink/GPU off bus), 45 (CUDA cleanup)

        Args:
            start_time: Filter start time (optional)
            end_time: Filter end time (optional)
            gpus: List of GPU IDs to filter

        Returns:
            DataFrame with XID error codes
        """
        return self.get_utilization("XID_ERRORS", start_time, end_time, gpus)

    def find_xid_errors_for_job(
        self,
        job_id: str,
        buffer_minutes: int = 1
    ) -> pd.DataFrame:
        """Find XID errors that occurred during a job's execution.

        Args:
            job_id: Job identifier
            buffer_minutes: Time buffer before/after job window

        Returns:
            DataFrame with XID errors in the job's time window
        """
        jobs = self.get_jobs()
        job = jobs[jobs["job_id"] == job_id]
        if job.empty:
            return pd.DataFrame()

        job = job.iloc[0]
        start = pd.to_datetime(job["start_time"], utc=True) - pd.Timedelta(minutes=buffer_minutes)
        end = pd.to_datetime(job["fail_time"] if pd.notna(job["fail_time"]) else job["end_time"], utc=True)
        end = end + pd.Timedelta(minutes=buffer_minutes)

        xid = self.get_xid_errors(str(start), str(end))

        # Filter to non-zero XID values
        if not xid.empty:
            xid_cols = [c for c in xid.columns if c != "Time"]
            # Keep only rows that have at least one non-zero XID
            row_mask = (xid[xid_cols] != 0).any(axis=1)
            xid = xid[row_mask]
            # Keep only GPU columns that have at least one non-zero XID
            if not xid.empty:
                active_cols = [c for c in xid_cols if (xid[c] != 0).any()]
                xid = xid[["Time"] + active_cols]

        return xid

    # =========================================================================
    # Ground Truth and Queries (for sampled data)
    # =========================================================================

    def get_ground_truth(self) -> pd.DataFrame:
        """Get ground truth labels (for sampled data).

        Schema: job_id, state, is_failure, category, reason, start_time,
                end_time, duration_sec, node_num, gpu_num, affected_node,
                affected_gpu, xid_count

        Returns:
            DataFrame with ground truth labels
        """
        if not self.sample_dir:
            raise ValueError("Ground truth only available for sampled data")

        path = self._get_data_path() / "ground_truth" / "labels.csv"
        return pd.read_csv(path)

    def get_queries(self, query_type: str = None) -> pd.DataFrame:
        """Get evaluation queries (for sampled data).

        Args:
            query_type: One of "detection", "localization", "analysis".
                       If None, returns all queries concatenated.

        Returns:
            DataFrame with evaluation queries
        """
        if not self.sample_dir:
            raise ValueError("Queries only available for sampled data")

        queries_path = self._get_data_path() / "queries"

        if query_type:
            path = queries_path / f"{query_type}.csv"
            return pd.read_csv(path)

        # Return all queries
        all_queries = []
        for qt in ["detection", "localization", "analysis"]:
            path = queries_path / f"{qt}.csv"
            if path.exists():
                df = pd.read_csv(path)
                df["query_type"] = qt
                all_queries.append(df)

        return pd.concat(all_queries, ignore_index=True) if all_queries else pd.DataFrame()

    # =========================================================================
    # Metadata
    # =========================================================================

    def get_gpu_list(self) -> List[str]:
        """Get list of GPU IDs from utilization data.

        Returns:
            List of GPU IDs (e.g., ["172.31.15.112-0", "172.31.15.112-1", ...])
        """
        util = self.get_gpu_util()
        if util.empty:
            return []
        return [c for c in util.columns if c != "Time"]

    def get_node_list(self) -> List[str]:
        """Get list of node IPs from utilization data.

        Returns:
            List of node IP addresses
        """
        gpus = self.get_gpu_list()
        nodes = set()
        for gpu in gpus:
            if "-" in gpu:
                node = gpu.rsplit("-", 1)[0]
                nodes.add(node)
        return sorted(nodes)

    def get_categories(self) -> List[str]:
        """Get list of failure categories (for sampled data).

        Returns:
            List: ["Infrastructure", "Framework", "Script"]
        """
        return ["Infrastructure", "Framework", "Script"]

    def get_reasons(self) -> List[str]:
        """Get list of failure reasons from ground truth (for sampled data).

        Returns:
            List of unique failure reasons
        """
        if not self.sample_dir:
            return []
        gt = self.get_ground_truth()
        return gt[gt["reason"].notna()]["reason"].unique().tolist()

    def get_stats(self) -> dict:
        """Get dataset statistics.

        Returns:
            Dictionary with dataset statistics
        """
        jobs = self.get_jobs()
        gpu_jobs = jobs[jobs["gpu_num"] > 0]
        failed = gpu_jobs[gpu_jobs["state"].isin(["FAILED", "TIMEOUT", "NODE_FAIL"])]

        stats = {
            "total_jobs": len(jobs),
            "gpu_jobs": len(gpu_jobs),
            "failed_jobs": len(failed),
            "failure_rate": len(failed) / len(gpu_jobs) if len(gpu_jobs) > 0 else 0,
            "gpus": len(self.get_gpu_list()),
            "nodes": len(self.get_node_list()),
        }

        if self.sample_dir:
            gt = self.get_ground_truth()
            stats["labeled_jobs"] = len(gt)
            stats["categories"] = gt["category"].value_counts().to_dict()

        return stats
