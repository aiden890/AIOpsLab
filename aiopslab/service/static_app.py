"""Interface to static dataset telemetry.

StaticApp: reads from local filesystem (used in tests).
DockerStaticApp: reads from inside a Docker container via `docker exec`.

Both provide fetch_*_df() methods that return DataFrames (used by actions
to save locally before returning paths to the agent).
"""

import subprocess
from io import StringIO
from datetime import datetime

import pandas as pd
from pathlib import Path


def _filter_by_time(df: pd.DataFrame, duration_minutes: int,
                    timestamp_col: str = "timestamp") -> pd.DataFrame:
    """Filter DataFrame to rows within the last N minutes.

    Returns all data if duration_minutes is 0/None or no rows match.
    """
    if df.empty or timestamp_col not in df.columns:
        return df
    if not duration_minutes:
        return df

    now = datetime.now().timestamp()
    cutoff = now - (duration_minutes * 60)
    filtered = df[df[timestamp_col] >= cutoff]

    if filtered.empty and not df.empty:
        return df

    return filtered


def _filter_logs(df: pd.DataFrame, service=None,
                 start_time=None, end_time=None) -> pd.DataFrame:
    """Apply service and time range filters to log data."""
    if service:
        service_col = None
        for col in ["cmdb_id", "service", "service_name"]:
            if col in df.columns:
                service_col = col
                break
        if service_col:
            df = df[df[service_col].str.contains(service, case=False, na=False)]

    if start_time and "timestamp" in df.columns:
        df = df[df["timestamp"] >= start_time]
    if end_time and "timestamp" in df.columns:
        df = df[df["timestamp"] <= end_time]

    return df


# ---------------------------------------------------------------------------
# StaticApp — reads from local filesystem (tests, fallback)
# ---------------------------------------------------------------------------

class StaticApp:
    """Service client for accessing static dataset telemetry on local filesystem."""

    def __init__(self, base_path: str):
        self.base_path = Path(base_path)

    def _get_namespace_path(self, namespace: str) -> Path:
        return self.base_path / namespace

    def _read_csv_files(self, directory: Path, pattern: str = "*.csv") -> pd.DataFrame:
        csv_files = sorted(directory.glob(pattern))
        if not csv_files:
            return pd.DataFrame()

        frames = []
        for f in csv_files:
            try:
                df = pd.read_csv(f)
                frames.append(df)
            except Exception as e:
                print(f"Warning: Failed to read {f}: {e}")

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    # -- DataFrame-returning methods (used by get_* actions) --

    def fetch_logs_df(self, namespace: str, service: str = None) -> pd.DataFrame:
        log_dir = self._get_namespace_path(namespace) / "logs"
        if not log_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(log_dir)
        return _filter_logs(df, service)

    def fetch_metrics_df(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(metric_dir)
        return _filter_by_time(df, duration_minutes) if duration_minutes else df

    def fetch_traces_df(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        trace_dir = self._get_namespace_path(namespace) / "traces"
        if not trace_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(trace_dir)
        return _filter_by_time(df, duration_minutes) if duration_minutes else df

    # -- Convenience string-returning methods --

    def get_logs(self, namespace: str, service: str = None,
                 start_time=None, end_time=None) -> str:
        log_dir = self._get_namespace_path(namespace) / "logs"
        if not log_dir.exists():
            return f"Error: No log data found for namespace '{namespace}'"
        df = self._read_csv_files(log_dir)
        if df.empty:
            return "No log data available."
        df = _filter_logs(df, service, start_time, end_time)
        if df.empty:
            return f"No logs found for service '{service}'"
        return df.to_string(index=False)

    def get_metrics(self, namespace: str, duration_minutes: int = 5) -> str:
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return f"Error: No metric data found for namespace '{namespace}'"
        df = self._read_csv_files(metric_dir)
        if df.empty:
            return "No metric data available."
        df = _filter_by_time(df, duration_minutes)
        if df.empty:
            return f"No metrics found within last {duration_minutes} minutes."
        return df.to_string(index=False)

    def get_traces(self, namespace: str, duration_minutes: int = 5) -> str:
        trace_dir = self._get_namespace_path(namespace) / "traces"
        if not trace_dir.exists():
            return f"Error: No trace data found for namespace '{namespace}'"
        df = self._read_csv_files(trace_dir)
        if df.empty:
            return "No trace data available."
        df = _filter_by_time(df, duration_minutes)
        if df.empty:
            return f"No traces found within last {duration_minutes} minutes."
        return df.to_string(index=False)

    def store_telemetry(self, namespace: str, telemetry_type: str,
                        data: pd.DataFrame, time_remapper=None) -> int:
        target_dir = self._get_namespace_path(namespace) / telemetry_type
        target_dir.mkdir(parents=True, exist_ok=True)
        if data.empty:
            return 0
        if time_remapper and "timestamp" in data.columns:
            data = data.copy()
            data["timestamp"] = data["timestamp"].apply(
                time_remapper.remap_timestamp
            )
        output_file = target_dir / f"{telemetry_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        data.to_csv(output_file, index=False)
        return len(data)

    def clear_telemetry(self, namespace: str):
        ns_dir = self._get_namespace_path(namespace)
        if ns_dir.exists():
            import shutil
            shutil.rmtree(ns_dir)


# ---------------------------------------------------------------------------
# DockerStaticApp — reads from inside a Docker container via docker exec
# ---------------------------------------------------------------------------

class DockerStaticApp:
    """Service client that reads telemetry from inside a Docker container.

    Uses `docker exec` to access data stored at /agent/telemetry/ inside
    the container, without exposing data to the host filesystem.
    """

    def __init__(self, container_name: str, data_path: str = "/agent/telemetry"):
        self.container_name = container_name
        self.data_path = data_path

    def _docker_exec(self, command: str) -> str:
        """Run a shell command inside the Docker container."""
        result = subprocess.run(
            ["docker", "exec", self.container_name, "bash", "-c", command],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout

    def _dir_exists(self, path: str) -> bool:
        """Check if a directory exists inside the container."""
        result = subprocess.run(
            ["docker", "exec", self.container_name, "test", "-d", path],
            capture_output=True,
        )
        return result.returncode == 0

    def _read_csv_files(self, directory: str) -> pd.DataFrame:
        """Read and concatenate all CSV files from a directory inside Docker."""
        ls_output = self._docker_exec(f"ls {directory}/*.csv 2>/dev/null")
        if not ls_output.strip():
            return pd.DataFrame()

        frames = []
        for csv_path in ls_output.strip().split("\n"):
            csv_path = csv_path.strip()
            if not csv_path:
                continue
            content = self._docker_exec(f"cat '{csv_path}'")
            if content.strip():
                try:
                    df = pd.read_csv(StringIO(content))
                    frames.append(df)
                except Exception as e:
                    print(f"Warning: Failed to parse {csv_path}: {e}")

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    # -- DataFrame-returning methods (used by get_* actions) --

    def fetch_logs_df(self, namespace: str, service: str = None) -> pd.DataFrame:
        log_dir = f"{self.data_path}/{namespace}/logs"
        if not self._dir_exists(log_dir):
            return pd.DataFrame()
        df = self._read_csv_files(log_dir)
        return _filter_logs(df, service)

    def fetch_metrics_df(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        metric_dir = f"{self.data_path}/{namespace}/metrics"
        if not self._dir_exists(metric_dir):
            return pd.DataFrame()
        df = self._read_csv_files(metric_dir)
        return _filter_by_time(df, duration_minutes) if duration_minutes else df

    def fetch_traces_df(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        trace_dir = f"{self.data_path}/{namespace}/traces"
        if not self._dir_exists(trace_dir):
            return pd.DataFrame()
        df = self._read_csv_files(trace_dir)
        return _filter_by_time(df, duration_minutes) if duration_minutes else df
