"""Interface to static dataset telemetry.

StaticApp: reads from local filesystem (used in tests).
DockerStaticApp: reads from inside a Docker container via `docker exec`.

Both provide fetch_*_df() methods that return DataFrames (used by actions
to save locally before returning paths to the agent).
"""

import subprocess
from io import StringIO
from datetime import datetime, timezone
from typing import Optional, Tuple

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


def _detect_col(df: pd.DataFrame, candidates: list) -> Optional[str]:
    """Return first matching column name from candidates."""
    return next((c for c in candidates if c in df.columns), None)


def _compute_log_overview(df: pd.DataFrame) -> dict:
    """Compute summary stats for a log DataFrame."""
    overview = {"total_rows": len(df)}

    if "timestamp" in df.columns:
        ts = df["timestamp"]
        overview["time_range"] = {
            "start": datetime.fromtimestamp(ts.min(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "end": datetime.fromtimestamp(ts.max(), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }

    service_col = _detect_col(df, ["cmdb_id", "service", "service_name"])
    if service_col:
        overview["rows_per_service"] = df[service_col].value_counts().to_dict()

    type_col = _detect_col(df, ["log_name", "level", "log_type", "type"])
    if type_col:
        overview["rows_per_log_type"] = df[type_col].value_counts().to_dict()

    return overview


def _search_in_df(df: pd.DataFrame, keyword: str, limit: int = 100) -> pd.DataFrame:
    """Search for keyword in text columns of a DataFrame."""
    text_col = _detect_col(df, ["value", "message", "log_message", "content", "body"])
    if text_col is None:
        return pd.DataFrame()
    mask = df[text_col].astype(str).str.contains(keyword, case=False, na=False, regex=False)
    return df[mask].head(limit)


def _compute_metric_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate metrics per service (min/avg/max for numeric columns)."""
    service_col = _detect_col(df, ["cmdb_id", "service", "service_name", "tc"])
    if not service_col:
        return df.describe().T

    numeric_cols = [
        c for c in df.select_dtypes(include="number").columns
        if c != "timestamp"
    ]
    if not numeric_cols:
        return pd.DataFrame()

    agg = df.groupby(service_col)[numeric_cols].agg(["mean", "min", "max"])
    agg.columns = [f"{col}_{stat}" for col, stat in agg.columns]
    return agg.round(2).reset_index()


def _compute_anomaly_metrics(
    df: pd.DataFrame,
    sr_threshold: float = 95.0,
    mrt_threshold: float = 500.0,
) -> pd.DataFrame:
    """Return per-service rows with anomaly flags based on success rate / response time."""
    service_col = _detect_col(df, ["cmdb_id", "service", "service_name", "tc"])
    if not service_col:
        return pd.DataFrame()

    rows = []
    for service, grp in df.groupby(service_col):
        row: dict = {"service": service, "data_points": len(grp)}

        if "sr" in df.columns:
            row["min_success_rate"] = round(float(grp["sr"].min()), 2)
            row["avg_success_rate"] = round(float(grp["sr"].mean()), 2)
            row["sr_anomaly"] = bool(grp["sr"].min() < sr_threshold)

        if "mrt" in df.columns:
            row["max_response_time_ms"] = round(float(grp["mrt"].max()), 2)
            row["avg_response_time_ms"] = round(float(grp["mrt"].mean()), 2)
            row["mrt_anomaly"] = bool(grp["mrt"].max() > mrt_threshold)

        if "rr" in df.columns:
            row["min_request_rate"] = round(float(grp["rr"].min()), 2)
            row["avg_request_rate"] = round(float(grp["rr"].mean()), 2)

        anomaly_flags = [v for k, v in row.items() if k.endswith("_anomaly")]
        row["is_anomaly"] = any(anomaly_flags)
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows)
    return result.sort_values("is_anomaly", ascending=False).reset_index(drop=True)


def _compute_trace_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate trace spans per service."""
    service_col = _detect_col(df, ["cmdb_id", "service", "service_name"])
    if not service_col or "duration" not in df.columns:
        return pd.DataFrame([{"total_spans": len(df)}])

    summary = (
        df.groupby(service_col)["duration"]
        .agg(
            span_count="count",
            avg_duration_ms="mean",
            max_duration_ms="max",
            p95_duration_ms=lambda x: x.quantile(0.95),
        )
        .round(2)
        .reset_index()
    )
    return summary.sort_values("avg_duration_ms", ascending=False).reset_index(drop=True)


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

    # -- New analytical methods --

    def fetch_log_overview(self, namespace: str) -> dict:
        """Return log summary stats (no raw data returned)."""
        log_dir = self._get_namespace_path(namespace) / "logs"
        if not log_dir.exists():
            return {}
        df = self._read_csv_files(log_dir)
        if df.empty:
            return {}
        return _compute_log_overview(df)

    def search_logs_df(self, namespace: str, keyword: str,
                       duration_minutes: int = None, limit: int = 100) -> pd.DataFrame:
        """Search log value field for a keyword."""
        log_dir = self._get_namespace_path(namespace) / "logs"
        if not log_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(log_dir)
        if df.empty:
            return pd.DataFrame()
        if duration_minutes:
            df = _filter_by_time(df, duration_minutes)
        return _search_in_df(df, keyword, limit)

    def fetch_metric_summary(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        """Return per-service aggregated metric stats."""
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(metric_dir)
        if df.empty:
            return pd.DataFrame()
        if duration_minutes:
            df = _filter_by_time(df, duration_minutes)
        return _compute_metric_summary(df)

    def fetch_anomaly_metrics(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        """Return services with degraded success rate or high response time."""
        metric_dir = self._get_namespace_path(namespace) / "metrics"
        if not metric_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(metric_dir)
        if df.empty:
            return pd.DataFrame()
        if duration_minutes:
            df = _filter_by_time(df, duration_minutes)
        return _compute_anomaly_metrics(df)

    def fetch_trace_summary(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        """Return aggregated trace stats per service."""
        trace_dir = self._get_namespace_path(namespace) / "traces"
        if not trace_dir.exists():
            return pd.DataFrame()
        df = self._read_csv_files(trace_dir)
        if df.empty:
            return pd.DataFrame()
        if duration_minutes:
            df = _filter_by_time(df, duration_minutes)
        return _compute_trace_summary(df)

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

    # Mapping from telemetry type name → single CSV filename used by the replayer
    _REPLAYER_FILE_MAP = {
        "logs": "log.csv",
        "metrics": "metric.csv",
        "traces": "trace.csv",
    }

    def _dir_exists(self, path: str) -> bool:
        """Check if a directory exists inside the container."""
        result = subprocess.run(
            ["docker", "exec", self.container_name, "test", "-d", path],
            capture_output=True,
        )
        return result.returncode == 0

    def _file_exists(self, path: str) -> bool:
        """Check if a file exists inside the container."""
        result = subprocess.run(
            ["docker", "exec", self.container_name, "test", "-f", path],
            capture_output=True,
        )
        return result.returncode == 0

    def _resolve_telemetry_dir(self, namespace: str,
                                type_name: str) -> Tuple[Optional[str], bool]:
        """Resolve where telemetry data lives inside the container.

        Tries two layouts in order:
          1. StaticDataset layout: {data_path}/{namespace}/{type_name}/ (directory)
          2. Replayer flat layout:  {data_path}/{file_name}            (single CSV)

        Returns:
            (path, is_flat) where is_flat=True means single-file replayer mode.
            Returns (None, False) if no data found.
        """
        # Try standard StaticDataset path: namespace subdirectory
        standard_dir = f"{self.data_path}/{namespace}/{type_name}"
        if self._dir_exists(standard_dir):
            return standard_dir, False

        # Fallback: replayer flat structure — single file at data_path root
        file_name = self._REPLAYER_FILE_MAP.get(type_name)
        if file_name:
            flat_file = f"{self.data_path}/{file_name}"
            if self._file_exists(flat_file):
                return self.data_path, True

        return None, False

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

    def _read_telemetry_df(self, namespace: str, type_name: str) -> pd.DataFrame:
        """Read telemetry data, auto-detecting StaticDataset vs replayer layout."""
        dir_path, is_flat = self._resolve_telemetry_dir(namespace, type_name)
        if dir_path is None:
            return pd.DataFrame()

        if is_flat:
            # Replayer mode: single CSV file
            file_name = self._REPLAYER_FILE_MAP[type_name]
            content = self._docker_exec(f"cat '{dir_path}/{file_name}'")
            if not content.strip():
                return pd.DataFrame()
            try:
                return pd.read_csv(StringIO(content))
            except Exception as e:
                print(f"Warning: Failed to parse replayer {file_name}: {e}")
                return pd.DataFrame()

        return self._read_csv_files(dir_path)

    # -- DataFrame-returning methods (used by get_* actions) --

    def fetch_logs_df(self, namespace: str, service: str = None,
                      limit: int = None) -> pd.DataFrame:
        df = self._read_telemetry_df(namespace, "logs")
        df = _filter_logs(df, service)
        return df.head(limit) if limit else df

    def fetch_metrics_df(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        df = self._read_telemetry_df(namespace, "metrics")
        return _filter_by_time(df, duration_minutes) if duration_minutes else df

    def fetch_traces_df(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        df = self._read_telemetry_df(namespace, "traces")
        return _filter_by_time(df, duration_minutes) if duration_minutes else df

    # -- New analytical methods --

    def fetch_log_overview(self, namespace: str) -> dict:
        """Return log summary stats without returning raw data."""
        df = self._read_telemetry_df(namespace, "logs")
        if df.empty:
            return {}
        return _compute_log_overview(df)

    def search_logs_df(self, namespace: str, keyword: str,
                       duration_minutes: int = None, limit: int = 100) -> pd.DataFrame:
        """Search log value field for a keyword."""
        df = self._read_telemetry_df(namespace, "logs")
        if df.empty:
            return pd.DataFrame()
        if duration_minutes:
            df = _filter_by_time(df, duration_minutes)
        return _search_in_df(df, keyword, limit)

    def fetch_metric_summary(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        """Return per-service aggregated metric stats."""
        df = self._read_telemetry_df(namespace, "metrics")
        if df.empty:
            return pd.DataFrame()
        if duration_minutes:
            df = _filter_by_time(df, duration_minutes)
        return _compute_metric_summary(df)

    def fetch_anomaly_metrics(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        """Return services with degraded success rate or high response time."""
        df = self._read_telemetry_df(namespace, "metrics")
        if df.empty:
            return pd.DataFrame()
        if duration_minutes:
            df = _filter_by_time(df, duration_minutes)
        return _compute_anomaly_metrics(df)

    def fetch_trace_summary(self, namespace: str, duration_minutes: int = None) -> pd.DataFrame:
        """Return aggregated trace stats per service."""
        df = self._read_telemetry_df(namespace, "traces")
        if df.empty:
            return pd.DataFrame()
        if duration_minutes:
            df = _filter_by_time(df, duration_minutes)
        return _compute_trace_summary(df)
