"""TelemetryHelper: wrapper injected into IPython kernel for Executor code.

Executor-generated Python code calls telemetry.get_*() to fetch raw data
from AIOpsLab's static actions, then reads the resulting CSV files with pandas.
"""


class TelemetryHelper:
    """Wraps StaticRCAActions methods for use inside IPython kernel.

    Injected as `telemetry` variable so Executor code can call:
        telemetry.get_logs(), telemetry.get_metrics(), telemetry.get_traces()
    """

    def __init__(self, actions_obj, namespace):
        """
        Args:
            actions_obj: StaticRCAActions instance with get_logs/metrics/traces.
            namespace: AIOpsLab namespace (e.g., "static-bank").
        """
        self._actions = actions_obj
        self._ns = namespace

    def get_logs(self, service=None):
        """Fetch logs → save to local CSV → return directory path.

        Usage in IPython:
            logs_dir = telemetry.get_logs()
            df = pd.read_csv(f"{logs_dir}/logs.csv")
        """
        if service:
            return self._actions.get_logs(self._ns, service)
        return self._actions.get_logs(self._ns)

    def get_metrics(self, duration=5):
        """Fetch metrics → save to local CSV → return directory path.

        Usage in IPython:
            metrics_dir = telemetry.get_metrics()
            df = pd.read_csv(f"{metrics_dir}/metrics.csv")
        """
        return self._actions.get_metrics(self._ns, duration)

    def get_traces(self, duration=5):
        """Fetch traces → save to local CSV → return directory path.

        Usage in IPython:
            traces_dir = telemetry.get_traces()
            df = pd.read_csv(f"{traces_dir}/traces.csv")
        """
        return self._actions.get_traces(self._ns, duration)
