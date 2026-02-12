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
            str: Path to the directory where logs are saved.
        """
        df = self.static_app.fetch_logs_df(namespace, service)
        if df.empty:
            filter_msg = f" for service '{service}'" if service else ""
            return f"No logs found{filter_msg} in namespace '{namespace}'"

        save_dir = os.path.join(os.getcwd(), "static_logs_output")
        os.makedirs(save_dir, exist_ok=True)
        df.to_csv(os.path.join(save_dir, "logs.csv"), index=False)

        return save_dir

    @read
    def get_metrics(self, namespace: str, duration: int = 5) -> str:
        """
        Collects metrics data from a static dataset and saves to local CSV files.

        Args:
            namespace (str): The namespace (e.g., "static-bank").
            duration (int): Minutes of data to retrieve.

        Returns:
            str: Path to the directory where metrics are saved.
        """
        df = self.static_app.fetch_metrics_df(namespace, duration)
        if df.empty:
            return f"No metrics found for namespace '{namespace}'"

        save_dir = os.path.join(os.getcwd(), "static_metrics_output")
        os.makedirs(save_dir, exist_ok=True)
        df.to_csv(os.path.join(save_dir, "metrics.csv"), index=False)

        print(f"Metrics data saved to: {save_dir}")
        return save_dir

    @read
    def get_traces(self, namespace: str, duration: int = 5) -> str:
        """
        Collects trace data from a static dataset and saves to local CSV files.

        Args:
            namespace (str): The namespace (e.g., "static-bank").
            duration (int): Minutes of data to retrieve.

        Returns:
            str: Path to the directory where traces are saved.
        """
        df = self.static_app.fetch_traces_df(namespace, duration)
        if df.empty:
            return f"No traces found for namespace '{namespace}'"

        save_dir = os.path.join(os.getcwd(), "static_traces_output")
        os.makedirs(save_dir, exist_ok=True)
        df.to_csv(os.path.join(save_dir, "traces.csv"), index=False)

        print(f"Trace data saved to: {save_dir}")
        return save_dir

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
        Execute a shell command on the host machine.
        Use this to process files saved by get_logs/get_metrics/get_traces.

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

        result = Shell.local_exec(command, timeout=timeout)

        return result
