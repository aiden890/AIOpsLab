"""TelemetryHelper: wrapper injected into IPython kernel for Executor code.

Executor-generated Python code calls telemetry.get_*() to fetch raw data
from AIOpsLab's static actions, then reads the resulting CSV files with pandas.
"""

from pathlib import Path


class TelemetryHelper:
    """Wraps StaticRCAActions methods for use inside IPython kernel.

    Injected as `telemetry` variable so Executor code can call:
        telemetry.get_logs(), telemetry.get_metrics(), telemetry.get_traces(),
        telemetry.list_metric_files(), telemetry.get_metric_file(filename)
    """

    def __init__(self, actions_obj, namespace,
                 enable_log=True, enable_metric=True, enable_trace=True):
        """
        Args:
            actions_obj: StaticRCAActions instance with get_logs/metrics/traces.
            namespace: AIOpsLab namespace (e.g., "static-bank").
            enable_log: Whether log telemetry is available.
            enable_metric: Whether metric telemetry is available.
            enable_trace: Whether trace telemetry is available.
        """
        self._actions = actions_obj
        self._ns = namespace
        self._enable_log = enable_log
        self._enable_metric = enable_metric
        self._enable_trace = enable_trace

    def get_logs(self, service=None):
        """Fetch logs → save to local CSV → return directory path.

        Usage in IPython:
            logs_path = telemetry.get_logs()
            df = pd.read_csv(logs_path)
        """
        if not self._enable_log:
            raise RuntimeError(
                "[Ablation] Logs are DISABLED in this configuration. "
                "Do not call telemetry.get_logs()."
            )
        if service:
            return self._actions.get_logs(self._ns, service)
        return self._actions.get_logs(self._ns)

    def get_metrics(self, start_time=None, end_time=None):
        """Fetch metrics → save to local CSV → return file path.

        Usage in IPython:
            metric_path = telemetry.get_metrics(start_time=start_time, end_time=end_time)
            df = pd.read_csv(metric_path)
        """
        if not self._enable_metric:
            raise RuntimeError(
                "[Ablation] Metrics are DISABLED in this configuration. "
                "Do not call telemetry.get_metrics()."
            )
        return self._actions.get_metrics(self._ns, start_time=start_time, end_time=end_time)

    def _metric_dir(self) -> Path:
        base_path = getattr(getattr(self._actions, "static_app", None), "base_path", None)
        if not base_path:
            raise RuntimeError("Metric file access unavailable: static dataset base path is missing.")
        return Path(base_path) / self._ns / "metrics"

    def list_metric_files(self) -> list[str]:
        """List available per-file metric CSVs under the dataset metrics directory."""
        if not self._enable_metric:
            raise RuntimeError(
                "[Ablation] Metrics are DISABLED in this configuration. "
                "Do not call telemetry.list_metric_files()."
            )
        metrics_dir = self._metric_dir()
        if not metrics_dir.exists():
            return []
        return sorted(p.name for p in metrics_dir.glob("metric_*.csv"))

    def get_metric_file(self, filename: str) -> str:
        """Return full path of a specific raw metric CSV (e.g., metric_service.csv).

        This enables per-file metric analysis instead of only merged metrics.csv.
        """
        if not self._enable_metric:
            raise RuntimeError(
                "[Ablation] Metrics are DISABLED in this configuration. "
                "Do not call telemetry.get_metric_file()."
            )
        name = str(filename or "").strip()
        if not name:
            raise ValueError("filename is required. Example: telemetry.get_metric_file('metric_service.csv').")
        if "/" in name or "\\" in name:
            raise ValueError("filename must be a file name only (no path separators).")
        if not name.endswith(".csv"):
            raise ValueError("filename must end with .csv")
        if not name.startswith("metric_"):
            raise ValueError("filename must start with 'metric_'")

        metrics_dir = self._metric_dir()
        file_path = metrics_dir / name
        if not file_path.exists():
            available = self.list_metric_files()
            raise FileNotFoundError(
                f"Metric file not found: {name}. Available: {available[:20]}"
            )
        return str(file_path)

    def get_traces(self, start_time=None, end_time=None):
        """Fetch traces → save to local CSV → return file path.

        Usage in IPython:
            trace_path = telemetry.get_traces(start_time=start_time, end_time=end_time)
            df = pd.read_csv(trace_path)
        """
        if not self._enable_trace:
            raise RuntimeError(
                "[Ablation] Traces are DISABLED in this configuration. "
                "Do not call telemetry.get_traces()."
            )
        return self._actions.get_traces(self._ns, start_time=start_time, end_time=end_time)
