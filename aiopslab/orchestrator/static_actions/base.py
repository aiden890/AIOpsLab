"""Base actions for static dataset problems.

Follows the same get/read pattern as the existing TaskActions:
  - get_*  : fetch from Docker container → save to local CSV → return path
  - read_* : read a local CSV file → return data as string

New analytical actions return formatted summaries inline (no file save needed).

Uses DockerStaticApp (production) or StaticApp (tests) as the service client.
"""

import os
import pandas as pd

from aiopslab.utils.actions import action, read, log_action, metric_action, trace_action
from aiopslab.service.static_app import StaticApp, DockerStaticApp
from aiopslab.service.shell import Shell

import re

LOG_COMMAND_PATTERN: str = (
    r"\b(?:"
    r"kubectl\s+(?:logs|get\s+events|describe|get\s+\S+\s+-w)"
    r"|docker\s+(?:logs|events)"
    r")\b(?:[^\n]*)"
)


class StaticTaskActions:
    """Base actions for static dataset problems.

    get_* fetches telemetry from Docker container, saves locally, returns path.
    read_* reads a saved local CSV file and returns its contents.
    Analytical actions (get_log_overview, get_anomaly_metrics, etc.) return
    inline formatted text — no file write needed.
    """

    def __init__(self, container_name: str = None, base_path: str = None,
                 work_dir: str = None):
        """
        Args:
            container_name: Docker container name (production mode).
            base_path: Local filesystem path (test/fallback mode).
            work_dir: Directory for saving telemetry CSV files.
                      Each parallel run should use a unique work_dir to avoid conflicts.
                      Defaults to cwd if not specified.
        """
        if container_name:
            self.static_app = DockerStaticApp(container_name)
        else:
            self.static_app = StaticApp(base_path or "/agent/telemetry")

        self.work_dir = work_dir or os.getcwd()

    # -------------------------------------------------------------------------
    # Discovery / overview actions (no file save — return inline text)
    # -------------------------------------------------------------------------

    @log_action
    def get_log_overview(self, namespace: str) -> str:
        """Compact summary of log data: time range, row counts per service and log type."""
        overview = self.static_app.fetch_log_overview(namespace)
        if not overview:
            return f"No log data found for namespace '{namespace}'"

        lines = [f"Log overview for namespace '{namespace}':"]
        lines.append(f"  Total rows: {overview.get('total_rows', 'N/A'):,}")

        tr = overview.get("time_range", {})
        if tr:
            lines.append(f"  Time range: {tr['start']} → {tr['end']} (UTC)")

        svc = overview.get("rows_per_service", {})
        if svc:
            lines.append("  Rows per service (cmdb_id):")
            for name, cnt in sorted(svc.items(), key=lambda x: -x[1]):
                lines.append(f"    {name}: {cnt:,}")

        lt = overview.get("rows_per_log_type", {})
        if lt:
            lines.append("  Rows per log type:")
            for name, cnt in sorted(lt.items(), key=lambda x: -x[1]):
                lines.append(f"    {name}: {cnt:,}")

        return "\n".join(lines)

    @metric_action
    def get_anomaly_metrics(self, namespace: str, start_time=None, end_time=None) -> str:
        """Anomaly report per service: flags low success rate (<95%) or high response time (>500ms). start_time/end_time: Unix timestamps (s)."""
        df = self.static_app.fetch_anomaly_metrics(namespace, start_time=start_time, end_time=end_time)
        if df.empty:
            return f"No metric data found for namespace '{namespace}'"

        lines = [f"Metric anomaly report for namespace '{namespace}'"]
        lines.append("")

        for _, row in df.iterrows():
            flags = []
            if row.get("sr_anomaly"):
                flags.append(f"LOW SUCCESS RATE ({row.get('min_success_rate')}%)")
            if row.get("mrt_anomaly"):
                flags.append(f"HIGH RESPONSE TIME ({row.get('max_response_time_ms')}ms)")
            status = " *** " + ", ".join(flags) if flags else " (normal)"
            lines.append(f"  {row['service']}{status}")
            if "avg_success_rate" in row:
                lines.append(f"    success_rate: min={row.get('min_success_rate')}% avg={row.get('avg_success_rate')}%")
            if "avg_response_time_ms" in row:
                lines.append(f"    response_time: avg={row.get('avg_response_time_ms')}ms max={row.get('max_response_time_ms')}ms")
            if "avg_request_rate" in row:
                lines.append(f"    request_rate: avg={row.get('avg_request_rate')}")

        return "\n".join(lines)

    @metric_action
    def get_metric_summary(self, namespace: str, start_time=None, end_time=None) -> str:
        """Aggregated metric stats per service (mean/min/max). start_time/end_time: Unix timestamps (s)."""
        df = self.static_app.fetch_metric_summary(namespace, start_time=start_time, end_time=end_time)
        if df.empty:
            return f"No metric data found for namespace '{namespace}'"
        return df.to_string(index=False)

    @trace_action
    def get_trace_summary(self, namespace: str, start_time=None, end_time=None) -> str:
        """Aggregated trace stats per service: span count, avg/max/p95 duration. start_time/end_time: Unix timestamps (s)."""
        df = self.static_app.fetch_trace_summary(namespace, start_time=start_time, end_time=end_time)
        if df.empty:
            return f"No trace data found for namespace '{namespace}'"
        return df.to_string(index=False)

    @log_action
    def search_logs(self, namespace: str, keyword: str = None,
                    start_time=None, end_time=None, limit: int = 100,
                    service: str = None) -> str:
        """Search logs by service name and/or keyword within a time window.

        Either `service` or `keyword` (or both) may be provided.
        - service: filter to a specific component (case-insensitive substring).
        - keyword: search across log text (value/message), cmdb_id, and log_name columns.

        Returns a summary (counts by service and log type) followed by the matching rows.
        """
        if keyword is None and service is None:
            return "Provide at least one of: keyword or service."

        df = self.static_app.search_logs_df(
            namespace, keyword,
            start_time=start_time, end_time=end_time,
            limit=limit, service=service,
        )

        query_desc = " | ".join(filter(None, [
            f"service='{service}'" if service else None,
            f"keyword='{keyword}'" if keyword else None,
        ]))

        if df.empty:
            return f"No log entries found in '{namespace}' for {query_desc}"

        # Summary header: counts by service and log type
        lines = [f"Log search [{query_desc}] in '{namespace}' — {len(df)} rows (limit={limit}):"]
        svc_col = next((c for c in ["cmdb_id", "service", "service_name"] if c in df.columns), None)
        type_col = next((c for c in ["log_name", "level", "log_type"] if c in df.columns), None)
        if svc_col:
            counts = df[svc_col].value_counts().to_dict()
            lines.append("  By service: " + ", ".join(f"{k}:{v}" for k, v in counts.items()))
        if type_col:
            counts = df[type_col].value_counts().to_dict()
            lines.append("  By log type: " + ", ".join(f"{k}:{v}" for k, v in counts.items()))
        lines.append("")
        lines.append(df.to_string(index=False))
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # get_* : fetch from source → save to local CSV → return path
    # -------------------------------------------------------------------------

    @log_action
    def get_logs(self, namespace: str, service: str = None,
                 limit: int = 500) -> str:
        """Fetches log data, saves to CSV, returns file path. Use read_logs() or exec_shell() to inspect."""
        df = self.static_app.fetch_logs_df(namespace, service, limit=limit)
        if df.empty:
            filter_msg = f" for service '{service}'" if service else ""
            return f"No logs found{filter_msg} in namespace '{namespace}'"

        save_dir = os.path.join(self.work_dir, "static_logs_output")
        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, "logs.csv")
        df.to_csv(file_path, index=False)

        return file_path

    @metric_action
    def get_metrics(self, namespace: str, start_time=None, end_time=None) -> str:
        """Fetches metrics data, saves to CSV, returns file path. start_time/end_time: Unix timestamps (s)."""
        df = self.static_app.fetch_metrics_df(namespace, start_time=start_time, end_time=end_time)
        if df.empty:
            raise RuntimeError(f"No metrics found for namespace '{namespace}'")

        save_dir = os.path.join(self.work_dir, "static_metrics_output")
        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, "metrics.csv")
        df.to_csv(file_path, index=False)

        return file_path

    @trace_action
    def get_traces(self, namespace: str, start_time=None, end_time=None) -> str:
        """Fetches trace data, saves to CSV, returns file path. start_time/end_time: Unix timestamps (s)."""
        df = self.static_app.fetch_traces_df(namespace, start_time=start_time, end_time=end_time)
        if df.empty:
            raise RuntimeError(f"No traces found for namespace '{namespace}'")

        save_dir = os.path.join(self.work_dir, "static_traces_output")
        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, "traces.csv")
        df.to_csv(file_path, index=False)

        return file_path

    # -------------------------------------------------------------------------
    # read_* : read a saved local CSV file → return data
    # -------------------------------------------------------------------------

    @staticmethod
    @log_action
    def read_logs(file_path: str, limit: int = 200, offset: int = 0) -> str:
        """Reads log CSV saved by get_logs(). Supports pagination via offset/limit."""
        if not os.path.exists(file_path):
            return f"error: Log file '{file_path}' not found."
        try:
            df = pd.read_csv(file_path)
            total = len(df)
            page = df.iloc[offset: offset + limit]
            header = f"Showing rows {offset}–{offset + len(page) - 1} of {total} total"
            if offset + limit < total:
                header += f" (use offset={offset + limit} for next page)"
            return header + "\n" + page.to_string(index=False)
        except Exception as e:
            return f"Failed to read logs: {str(e)}"

    @staticmethod
    @metric_action
    def read_metrics(file_path: str, limit: int = 200, offset: int = 0) -> str:
        """Reads metrics CSV saved by get_metrics(). Supports pagination via offset/limit."""
        if not os.path.exists(file_path):
            return f"error: Metrics file '{file_path}' not found."
        try:
            df = pd.read_csv(file_path)
            total = len(df)
            page = df.iloc[offset: offset + limit]
            header = f"Showing rows {offset}–{offset + len(page) - 1} of {total} total"
            if offset + limit < total:
                header += f" (use offset={offset + limit} for next page)"
            return header + "\n" + page.to_string(index=False)
        except Exception as e:
            return f"Failed to read metrics: {str(e)}"

    @staticmethod
    @trace_action
    def read_traces(file_path: str, limit: int = 200, offset: int = 0) -> str:
        """Reads traces CSV saved by get_traces(). Supports pagination via offset/limit."""
        if not os.path.exists(file_path):
            return f"error: Traces file '{file_path}' not found."
        try:
            df = pd.read_csv(file_path)
            total = len(df)
            page = df.iloc[offset: offset + limit]
            header = f"Showing rows {offset}–{offset + len(page) - 1} of {total} total"
            if offset + limit < total:
                header += f" (use offset={offset + limit} for next page)"
            return header + "\n" + page.to_string(index=False)
        except Exception as e:
            return f"Failed to read traces: {str(e)}"

    # -------------------------------------------------------------------------
    # Shell access (restricted)
    # -------------------------------------------------------------------------

    @action
    def exec_shell(self, command: str, timeout: int = 30) -> str:
        """Runs a shell command. Restricted to: static_logs_output, static_metrics_output, static_traces_output."""
        BLOCK_LIST = {
            "kubectl edit": "Error: Cannot use `kubectl edit`. Use `kubectl patch` instead.",
            "kubectl port-forward": "Error: Cannot use `kubectl port-forward`.",
            "docker logs -f": "Error: Cannot use `docker logs -f`. Use `docker logs` instead.",
        }
        for pattern, error in BLOCK_LIST.items():
            if pattern in command:
                return error

        # Validate path access - only allow telemetry output directories
        validation_error = self._validate_path_access(command)
        if validation_error:
            return validation_error

        result = Shell.local_exec(command, timeout=timeout)

        return result

    def _validate_path_access(self, command: str) -> str | None:
        """
        Validate that command only accesses allowed telemetry directories.

        Allowed directories are relative to self.work_dir:
        - {work_dir}/static_logs_output
        - {work_dir}/static_metrics_output
        - {work_dir}/static_traces_output

        Args:
            command: The shell command to validate

        Returns:
            Error message if validation fails, None if valid
        """
        ALLOWED_DIRS = [
            os.path.join(self.work_dir, "static_logs_output"),
            os.path.join(self.work_dir, "static_metrics_output"),
            os.path.join(self.work_dir, "static_traces_output"),
        ]

        # Block dangerous patterns
        DANGEROUS_PATTERNS = [
            (r'\.\./|/\.\./', "Parent directory traversal (..) is not allowed"),
            (r'/etc/|/var/|/sys/|/proc/', "System directory access is not allowed"),
            (r'\$HOME|\$USER', "Environment variable expansion is not allowed"),
        ]

        for pattern, error_msg in DANGEROUS_PATTERNS:
            if re.search(pattern, command):
                return f"Error: {error_msg}"

        # Extract potential file paths from command (absolute and relative)
        path_candidates = re.findall(r'/?[a-zA-Z0-9_\-/.]+/[a-zA-Z0-9_\-/.]+', command)
        for path in path_candidates:
            resolved = os.path.abspath(path)
            is_allowed = any(
                resolved == d or resolved.startswith(d + os.sep)
                for d in ALLOWED_DIRS
            )
            if not is_allowed:
                return (
                    f"Error: Access denied to '{path}'.\n"
                    f"Commands can only access these directories:\n"
                    f"  - {', '.join(ALLOWED_DIRS)}"
                )

        return None
