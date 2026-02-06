# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Bank Dataset loader for OpenRCA."""

import os
from pathlib import Path
from datetime import datetime
from typing import Optional, Union

import pandas as pd

from aiopslab.paths import OPENRCA_DATASET_DIR


class BankDataset:
    """Dataset loader for OpenRCA Bank domain.

    Provides access to:
    - Metrics (container, app)
    - Traces
    - Logs
    - Records (ground truth)
    - Queries (evaluation tasks)

    All telemetry methods support optional time filtering.
    """

    def __init__(self, date: str = None):
        """Initialize BankDataset.

        Args:
            date: Date folder to load (e.g., "2021_03_04").
                  If None, must specify date when calling telemetry methods.
        """
        self.base_path = OPENRCA_DATASET_DIR / "Bank"
        self.date = date
        self._cached_data = {}

    def _get_telemetry_path(self, date: str = None) -> Path:
        """Get telemetry path for given date."""
        date = date or self.date
        if date is None:
            raise ValueError("Date must be specified either in constructor or method call")
        return self.base_path / "telemetry" / date

    def _parse_timestamp(self, ts: Union[str, int, float]) -> float:
        """Parse timestamp to Unix epoch seconds.

        Args:
            ts: Timestamp as string (datetime), int, or float (Unix epoch)

        Returns:
            Unix epoch seconds as float
        """
        if isinstance(ts, (int, float)):
            # Already Unix timestamp (might be in seconds or milliseconds)
            if ts > 1e12:  # Likely milliseconds
                return ts / 1000
            return float(ts)
        elif isinstance(ts, str):
            # Parse datetime string
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(ts, fmt)
                    return dt.timestamp()
                except ValueError:
                    continue
            raise ValueError(f"Cannot parse timestamp: {ts}")
        else:
            raise TypeError(f"Unsupported timestamp type: {type(ts)}")

    def _filter_by_time(
        self,
        df: pd.DataFrame,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        timestamp_col: str = "timestamp"
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

        # Get timestamp column and convert if needed
        ts_col = df[timestamp_col].copy()

        # Handle millisecond timestamps
        if ts_col.dtype in ['int64', 'float64'] and ts_col.iloc[0] > 1e12:
            ts_col = ts_col / 1000

        if start_time is not None:
            start_ts = self._parse_timestamp(start_time)
            df = df[ts_col >= start_ts]
            ts_col = ts_col[ts_col >= start_ts]

        if end_time is not None:
            end_ts = self._parse_timestamp(end_time)
            df = df[ts_col <= end_ts]

        return df

    def get_metric_container(
        self,
        start_time: str = None,
        end_time: str = None,
        date: str = None
    ) -> pd.DataFrame:
        """Get container metrics.

        Schema: timestamp, cmdb_id, kpi_name, value

        Args:
            start_time: Filter start time (optional)
            end_time: Filter end time (optional)
            date: Date folder (optional, uses default if not specified)

        Returns:
            DataFrame with container metrics
        """
        path = self._get_telemetry_path(date) / "metric" / "metric_container.csv"
        df = pd.read_csv(path)
        return self._filter_by_time(df, start_time, end_time)

    def get_metric_app(
        self,
        start_time: str = None,
        end_time: str = None,
        date: str = None
    ) -> pd.DataFrame:
        """Get app metrics.

        Schema: timestamp, rr, sr, cnt, mrt, tc

        Args:
            start_time: Filter start time (optional)
            end_time: Filter end time (optional)
            date: Date folder (optional, uses default if not specified)

        Returns:
            DataFrame with app metrics
        """
        path = self._get_telemetry_path(date) / "metric" / "metric_app.csv"
        df = pd.read_csv(path)
        return self._filter_by_time(df, start_time, end_time)

    def get_traces(
        self,
        start_time: str = None,
        end_time: str = None,
        date: str = None
    ) -> pd.DataFrame:
        """Get trace spans.

        Schema: timestamp, cmdb_id, parent_id, span_id, trace_id, duration

        Args:
            start_time: Filter start time (optional)
            end_time: Filter end time (optional)
            date: Date folder (optional, uses default if not specified)

        Returns:
            DataFrame with trace spans
        """
        path = self._get_telemetry_path(date) / "trace" / "trace_span.csv"
        df = pd.read_csv(path)
        return self._filter_by_time(df, start_time, end_time)

    def get_logs(
        self,
        start_time: str = None,
        end_time: str = None,
        date: str = None
    ) -> pd.DataFrame:
        """Get service logs.

        Schema: log_id, timestamp, cmdb_id, log_name, value

        Args:
            start_time: Filter start time (optional)
            end_time: Filter end time (optional)
            date: Date folder (optional, uses default if not specified)

        Returns:
            DataFrame with logs
        """
        path = self._get_telemetry_path(date) / "log" / "log_service.csv"
        df = pd.read_csv(path)
        return self._filter_by_time(df, start_time, end_time)

    def get_records(self) -> pd.DataFrame:
        """Get ground truth records.

        Schema: level, component, timestamp, datetime, reason

        Returns:
            DataFrame with fault records (ground truth)
        """
        path = self.base_path / "record.csv"
        return pd.read_csv(path)

    def get_queries(self) -> pd.DataFrame:
        """Get evaluation queries/tasks.

        Schema: task_index, instruction, scoring_points

        Returns:
            DataFrame with evaluation tasks
        """
        path = self.base_path / "query.csv"
        return pd.read_csv(path)

    def get_available_dates(self) -> list:
        """Get list of available date folders.

        Returns:
            List of date strings (e.g., ["2021_03_04", "2021_03_05", ...])
        """
        telemetry_path = self.base_path / "telemetry"
        dates = [d.name for d in telemetry_path.iterdir()
                 if d.is_dir() and not d.name.startswith('.')]
        return sorted(dates)

    def get_components(self) -> list:
        """Get list of unique components from records.

        Returns:
            List of component names
        """
        records = self.get_records()
        return records["component"].unique().tolist()

    def get_reasons(self) -> list:
        """Get list of unique failure reasons from records.

        Returns:
            List of reason strings
        """
        records = self.get_records()
        return records["reason"].unique().tolist()
