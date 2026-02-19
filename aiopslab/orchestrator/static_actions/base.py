"""Base actions for static dataset problems.

Follows the same get/read pattern as the existing TaskActions:
  - get_*  : fetch from Docker container → save to local CSV → return path
  - read_* : read a local CSV file → return data as string

New analytical actions return formatted summaries inline (no file save needed).

Uses DockerStaticApp (production) or StaticApp (tests) as the service client.
"""

import os
import pandas as pd

from aiopslab.utils.actions import action, read
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

    def __init__(self, container_name: str = None, base_path: str = None):
        """
        Args:
            container_name: Docker container name (production mode).
            base_path: Local filesystem path (test/fallback mode).
        """
        if container_name:
            self.static_app = DockerStaticApp(container_name)
        else:
            self.static_app = StaticApp(base_path or "/agent/telemetry")

    # -------------------------------------------------------------------------
    # Discovery / overview actions (no file save — return inline text)
    # -------------------------------------------------------------------------

    @read
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

    @read
    def get_anomaly_metrics(self, namespace: str, duration: int = None) -> str:
        """Anomaly report per service: flags low success rate (<95%) or high response time (>500ms)."""
        df = self.static_app.fetch_anomaly_metrics(namespace, duration)
        if df.empty:
            msg = f"No metric data found for namespace '{namespace}'"
            if duration:
                msg += f" in the last {duration} minutes"
            return msg

        lines = [f"Metric anomaly report for namespace '{namespace}'"]
        if duration:
            lines[0] += f" (last {duration} min)"
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

    @read
    def get_metric_summary(self, namespace: str, duration: int = None) -> str:
        """Aggregated metric stats per service (mean/min/max). More compact than raw rows."""
        df = self.static_app.fetch_metric_summary(namespace, duration)
        if df.empty:
            return f"No metric data found for namespace '{namespace}'"
        return df.to_string(index=False)

    @read
    def get_trace_summary(self, namespace: str, duration: int = None) -> str:
        """Aggregated trace stats per service: span count, avg/max/p95 duration."""
        df = self.static_app.fetch_trace_summary(namespace, duration)
        if df.empty:
            return f"No trace data found for namespace '{namespace}'"
        return df.to_string(index=False)

    @read
    def search_logs(self, namespace: str, keyword: str,
                    duration: int = None, limit: int = 100) -> str:
        """Case-insensitive keyword search over logs. Returns matching rows."""
        df = self.static_app.search_logs_df(namespace, keyword, duration, limit)
        if df.empty:
            msg = f"No log entries matching '{keyword}' found in namespace '{namespace}'"
            if duration:
                msg += f" (last {duration} min)"
            return msg
        header = f"Log search results for '{keyword}' in '{namespace}'"
        if duration:
            header += f" (last {duration} min)"
        header += f" — {len(df)} rows (limit={limit}):"
        return header + "\n" + df.to_string(index=False)

    # -------------------------------------------------------------------------
    # get_* : fetch from source → save to local CSV → return path
    # -------------------------------------------------------------------------

    @read
    def get_logs(self, namespace: str, service: str = None,
                 limit: int = 500) -> str:
        """Fetches log data, saves to CSV, returns file path. Use read_logs() or exec_shell() to inspect."""
        df = self.static_app.fetch_logs_df(namespace, service, limit=limit)
        if df.empty:
            filter_msg = f" for service '{service}'" if service else ""
            return f"No logs found{filter_msg} in namespace '{namespace}'"

        save_dir = os.path.join(os.getcwd(), "static_logs_output")
        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, "logs.csv")
        df.to_csv(file_path, index=False)

        return file_path

    @read
    def get_metrics(self, namespace: str, duration: int = 5) -> str:
        """Fetches metrics data, saves to CSV, returns file path. Use read_metrics() to inspect."""
        df = self.static_app.fetch_metrics_df(namespace, duration)
        if df.empty:
            return f"No metrics found for namespace '{namespace}'"

        save_dir = os.path.join(os.getcwd(), "static_metrics_output")
        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, "metrics.csv")
        df.to_csv(file_path, index=False)

        print(f"Metrics data saved to: {file_path}")
        return file_path

    @read
    def get_traces(self, namespace: str, duration: int = 5) -> str:
        """Fetches trace data, saves to CSV, returns file path. Use read_traces() to inspect."""
        df = self.static_app.fetch_traces_df(namespace, duration)
        if df.empty:
            return f"No traces found for namespace '{namespace}'"

        save_dir = os.path.join(os.getcwd(), "static_traces_output")
        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, "traces.csv")
        df.to_csv(file_path, index=False)

        print(f"Trace data saved to: {file_path}")
        return file_path

    # -------------------------------------------------------------------------
    # read_* : read a saved local CSV file → return data
    # -------------------------------------------------------------------------

    @staticmethod
    @read
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
    @read
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
    @read
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

        Allowed directories:
        - static_logs_output
        - static_metrics_output
        - static_traces_output

        Args:
            command: The shell command to validate

        Returns:
            Error message if validation fails, None if valid
        """
        ALLOWED_DIRS = [
            "static_logs_output",
            "static_metrics_output",
            "static_traces_output"
        ]

        # Block dangerous patterns
        DANGEROUS_PATTERNS = [
            (r'\.\./|/\.\./', "Parent directory traversal (..) is not allowed"),
            (r'~|/Users/|/home/|/root/', "Home directory access is not allowed"),
            (r'/etc/|/var/|/sys/|/proc/', "System directory access is not allowed"),
            (r'\$HOME|\$USER', "Environment variable expansion is not allowed"),
        ]

        for pattern, error_msg in DANGEROUS_PATTERNS:
            if re.search(pattern, command):
                return f"Error: {error_msg}"

        # Extract potential file paths from command.
        # Only flag /path that starts at a word boundary (genuine absolute paths),
        # not slashes inside relative paths like static_traces_output/traces.csv
        # or awk expressions like sum/NR.
        absolute_paths = re.findall(r'(?<!\w)/[a-zA-Z0-9_\-/.]+', command)
        if absolute_paths:
            return (
                f"Error: Absolute paths are not allowed.\n"
                f"Only relative paths within these directories are permitted:\n"
                f"  - {', '.join(ALLOWED_DIRS)}\n"
                f"Blocked: {', '.join(absolute_paths)}"
            )

        # Extract words that might be paths
        words = command.split()
        for word in words:
            # Skip flags, commands, and empty strings
            if not word or word.startswith('-'):
                continue

            # Skip common shell commands
            if word in ['ls', 'cat', 'grep', 'head', 'tail', 'wc', 'find', 'awk', 'sed', 'cut', 'sort', 'uniq', 'less', 'more', 'echo', 'pwd']:
                continue

            # Skip quoted strings (likely search patterns or arguments)
            if word.startswith('"') or word.startswith("'") or word.startswith('|') or word.startswith('>'):
                continue

            # If word looks like a path (contains / or matches directory names)
            if '/' in word or any(word.startswith(d) for d in ALLOWED_DIRS):
                # Check if it's in an allowed directory
                is_allowed = any(
                    word == allowed_dir or word.startswith(f"{allowed_dir}/")
                    for allowed_dir in ALLOWED_DIRS
                )

                if not is_allowed:
                    return (
                        f"Error: Access denied to '{word}'.\n"
                        f"Commands can only access these directories:\n"
                        f"  - {', '.join(ALLOWED_DIRS)}"
                    )

        return None
