"""TelemetryHelper: wrapper injected into IPython kernel for Executor code.

Executor-generated Python code calls telemetry.get_*() to fetch raw data
from AIOpsLab's static actions, then reads the resulting CSV files with pandas.
"""


class TelemetryHelper:
    """Wraps StaticRCAActions methods for use inside IPython kernel.

    Injected as `telemetry` variable so Executor code can call:
        telemetry.get_logs(), telemetry.get_metrics(), telemetry.get_traces()
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
            logs_dir = telemetry.get_logs()
            df = pd.read_csv(f"{logs_dir}/logs.csv")
        """
        if not self._enable_log:
            raise RuntimeError(
                "[Ablation] Logs are DISABLED in this configuration. "
                "Do not call telemetry.get_logs()."
            )
        if service:
            return self._actions.get_logs(self._ns, service)
        return self._actions.get_logs(self._ns)

    def get_metrics(self, duration=5):
        """Fetch metrics → save to local CSV → return directory path.

        Usage in IPython:
            metrics_dir = telemetry.get_metrics()
            df = pd.read_csv(f"{metrics_dir}/metrics.csv")
        """
        if not self._enable_metric:
            raise RuntimeError(
                "[Ablation] Metrics are DISABLED in this configuration. "
                "Do not call telemetry.get_metrics()."
            )
        return self._actions.get_metrics(self._ns, duration)

    def get_traces(self, duration=5):
        """Fetch traces → save to local CSV → return directory path.

        Usage in IPython:
            traces_dir = telemetry.get_traces()
            df = pd.read_csv(f"{traces_dir}/traces.csv")
        """
        if not self._enable_trace:
            raise RuntimeError(
                "[Ablation] Traces are DISABLED in this configuration. "
                "Do not call telemetry.get_traces()."
            )
        return self._actions.get_traces(self._ns, duration)
