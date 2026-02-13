"""Base actions for static dataset problems.

Follows the same get/read pattern as the existing TaskActions:
  - get_*  : fetch from Docker container → save to local CSV → return path
  - read_* : read a local CSV file → return data as string

Uses DockerStaticApp (production) or StaticApp (tests) as the service client.
"""

import os
import pandas as pd

from aiopslab.utils.actions import action, read, write
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

    # ----- get_* : fetch from source → save to local → return path -----

    @read
    def get_logs(self, namespace: str, service: str = None) -> str:
        """
        Collects log data from a static dataset and saves to local CSV files.

        Args:
            namespace (str): The namespace (e.g., "static-bank").
            service (str, optional): The service name to filter by. If omitted, returns all logs.

        Returns:
            str: Path to the saved log file (CSV format).
        """
        df = self.static_app.fetch_logs_df(namespace, service)
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
        """
        Collects metrics data from a static dataset and saves to local CSV files.

        Args:
            namespace (str): The namespace (e.g., "static-bank").
            duration (int): Minutes of data to retrieve.

        Returns:
            str: Path to the saved metrics file (CSV format).
        """
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
        """
        Collects trace data from a static dataset and saves to local CSV files.

        Args:
            namespace (str): The namespace (e.g., "static-bank").
            duration (int): Minutes of data to retrieve.

        Returns:
            str: Path to the saved traces file (CSV format).
        """
        df = self.static_app.fetch_traces_df(namespace, duration)
        if df.empty:
            return f"No traces found for namespace '{namespace}'"

        save_dir = os.path.join(os.getcwd(), "static_traces_output")
        os.makedirs(save_dir, exist_ok=True)
        file_path = os.path.join(save_dir, "traces.csv")
        df.to_csv(file_path, index=False)

        print(f"Trace data saved to: {file_path}")
        return file_path

    # ----- read_* : read a saved local CSV file → return data -----

    @staticmethod
    @read
    def read_logs(file_path: str) -> str:
        """
        Reads and returns log data from a specified CSV file.

        Args:
            file_path (str): Path to the log file (CSV format).

        Returns:
            str: The log data or an error message.
        """
        if not os.path.exists(file_path):
            return f"error: Log file '{file_path}' not found."
        try:
            df = pd.read_csv(file_path)
            return df.to_string(index=False)
        except Exception as e:
            return f"Failed to read logs: {str(e)}"

    @staticmethod
    @read
    def read_metrics(file_path: str) -> str:
        """
        Reads and returns metrics from a specified CSV file.

        Args:
            file_path (str): Path to the metrics file (CSV format).

        Returns:
            str: The requested metrics or an error message.
        """
        if not os.path.exists(file_path):
            return f"error: Metrics file '{file_path}' not found."
        try:
            df = pd.read_csv(file_path)
            return df.to_string(index=False)
        except Exception as e:
            return f"Failed to read metrics: {str(e)}"

    @staticmethod
    @read
    def read_traces(file_path: str) -> str:
        """
        Reads and returns traces from a specified CSV file.

        Args:
            file_path (str): Path to the traces file (CSV format).

        Returns:
            str: The requested traces or an error message.
        """
        if not os.path.exists(file_path):
            return f"error: Traces file '{file_path}' not found."
        try:
            df = pd.read_csv(file_path)
            return df.to_string(index=False)
        except Exception as e:
            return f"Failed to read traces: {str(e)}"

    # ----- other actions -----

    @action
    def exec_shell(self, command: str, timeout: int = 30) -> str:
        """
        Execute a shell command with restricted access to telemetry directories only.
        Agent can only access: static_logs_output, static_metrics_output, static_traces_output

        Args:
            command (str): The command to execute.
            timeout (int): Timeout in seconds. Default is 30.

        Returns:
            str: The output of the command.
        """
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

        # Extract potential file paths from command
        # Look for absolute paths
        absolute_paths = re.findall(r'/[a-zA-Z0-9_\-/.]+', command)
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
