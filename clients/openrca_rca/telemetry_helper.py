"""TelemetryHelper: wrapper injected into IPython kernel for Executor code.

Executor-generated Python code calls telemetry.get_*() to fetch raw data
from AIOpsLab's static actions, then reads the resulting CSV files with pandas.
"""

import os


class TelemetryHelper:
    """Wraps StaticRCAActions methods for use inside IPython kernel.

    Injected as `telemetry` variable so Executor code can call:
        telemetry.get_logs(), telemetry.get_metrics(), telemetry.get_traces()

    Returns file path (str) or None if no data is available.
    """

    def __init__(self, actions_obj, namespace):
        """
        Args:
            actions_obj: StaticRCAActions instance with get_logs/metrics/traces.
            namespace: AIOpsLab namespace (e.g., "static-bank").
        """
        self._actions = actions_obj
        self._ns = namespace

    @staticmethod
    def _check_empty(file_path):
        """Return None if the CSV file is empty (no data), else return path."""
        try:
            if not os.path.exists(file_path):
                return None
            if os.path.getsize(file_path) <= 1:
                return None
            return file_path
        except Exception:
            return None

    def get_logs(self, service=None):
        """Fetch logs → save to local CSV → return file path or None.

        Returns:
            str: File path to CSV, or None if no log data is available.
        """
        if service:
            path = self._actions.get_logs(self._ns, service)
        else:
            path = self._actions.get_logs(self._ns)
        return self._check_empty(path)

    def get_metrics(self, duration=5):
        """Fetch metrics → save to local CSV → return file path or None.

        Returns:
            str: File path to CSV, or None if no metric data is available.
        """
        path = self._actions.get_metrics(self._ns, duration)
        return self._check_empty(path)

    def get_traces(self, duration=5):
        """Fetch traces → save to local CSV → return file path or None.

        Returns:
            str: File path to CSV, or None if no trace data is available.
        """
        path = self._actions.get_traces(self._ns, duration)
        return self._check_empty(path)
