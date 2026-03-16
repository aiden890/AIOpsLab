"""RE2-TT Dataset Application.

Manages a single RE2-TT fault injection case as a Docker-based telemetry source.
Uses the same static_dataset Docker container but runs process_telemetry_re2tt.py.

Each case directory contains:
  simple_metrics.csv  - wide format metrics (time in Unix seconds)
  logs.csv            - log records (timestamp in nanoseconds)
  traces.csv          - span records (startTimeMillis in ms)
  logts.csv           - log event time series (time in Unix seconds, 15s intervals)
  inject_time.txt     - Unix timestamp of fault injection
  cluster_info.json   - log template metadata
"""

import json
import os
import tempfile
from pathlib import Path

from aiopslab.service.dock import Docker
from aiopslab.paths import TARGET_MICROSERVICES, STATIC_DATASET_METADATA

# Known RE2-TT fault types and their human-readable reasons
FAULT_REASON_MAP = {
    "cpu":    "cpu stress",
    "mem":    "memory stress",
    "delay":  "network delay",
    "loss":   "packet loss",
    "disk":   "disk I/O stress",
    "socket": "socket exhaustion",
}

# All possible fault reasons across RE2-TT
ALL_FAULT_REASONS = list(FAULT_REASON_MAP.values())


def _align_to_30min(inject_time: int):
    """Return a 30-minute window aligned to the slot containing inject_time.

    Examples:
      13:20 → (13:00, 13:30)
      15:44 → (15:30, 16:00)
    """
    slot = 30 * 60  # 1800 seconds
    window_start = (inject_time // slot) * slot
    window_end = window_start + slot
    return window_start, window_end


class RE2TTDataset:
    """Application class for one RE2-TT fault injection case."""

    def __init__(self, case_path: Path, namespace: str, inject_time: int,
                 services: list, fault_service: str, fault_type: str,
                 window_start: int = None, window_end: int = None):
        """
        Args:
            case_path:     Absolute path to the case directory.
            namespace:     Unique Docker namespace for this case.
            inject_time:   Unix timestamp (seconds) of fault injection.
            services:      List of service names in this system.
            fault_service: Ground-truth root cause service.
            fault_type:    Ground-truth fault type (cpu/mem/delay/loss/disk/socket).
            window_start:  Start of telemetry window (Unix seconds).
                           Defaults to 30-min aligned slot containing inject_time.
            window_end:    End of telemetry window (Unix seconds).
                           Defaults to window_start + 30 min.
        """
        self.case_path = Path(case_path)
        self.namespace = namespace
        self.inject_time = inject_time
        self.fault_service = fault_service
        self.fault_type = fault_type

        # Time window: 30-min aligned slot by default
        if window_start is not None and window_end is not None:
            self._init_start = window_start
            self._init_end = window_end
        else:
            self._init_start, self._init_end = _align_to_30min(inject_time)

        # Docker setup — same container image as static_dataset
        self.docker = Docker()
        self.docker_deploy_path = TARGET_MICROSERVICES / "static_dataset"
        self.dataset_path = self.case_path
        self._processing_config_path = None
        self.condition = None
        self.query_info = None

        # Build dataset_config (same structure as static_dataset configs)
        self.dataset_config = {
            "dataset_name": "RE2-TT Train Ticket",
            "dataset_type": "re2tt",
            "namespace": namespace,
            "telemetry": {
                "enable_log":    True,
                "enable_metric": True,
                "enable_trace":  True,
            },
            "executor": {"enable": True},
            "data_mapping": {
                "metric_files": ["metrics.csv"],
                "log_files":    ["logs.csv"],
                "trace_files":  ["traces.csv"],
            },
            "services": services,
            "possible_root_causes": {
                "components": services,
                "reasons":    ALL_FAULT_REASONS,
            },
        }

    # ------------------------------------------------------------------
    # Application interface
    # ------------------------------------------------------------------

    def get_services(self) -> list:
        return self.dataset_config.get("services", [])

    def get_container_name(self) -> str:
        return f"static-dataset-{self.namespace}"

    def get_app_summary(self) -> str:
        return (
            f"Service Name: Train Ticket\n"
            f"Namespace: {self.namespace}\n"
            f"Description: A microservice-based online train ticketing system "
            f"running on Kubernetes. An incident has been detected.\n"
            f"Available services: {', '.join(self.get_services())}"
        )

    # ------------------------------------------------------------------
    # Lifecycle: deploy / cleanup
    # ------------------------------------------------------------------

    def deploy(self):
        """Start Docker container and load RE2-TT telemetry into it.

        1. Write processing config to a temp file.
        2. Start Docker container with case directory bind-mounted.
        3. Run process_telemetry_re2tt.py --mode init (synchronous).
        4. Stream mode is a no-op for RE2-TT (static snapshot).
        """
        print(f"Deploying RE2-TT case: {self.case_path.name} → {self.namespace}")

        processing_config = {
            "namespace": self.namespace,
            "time_offset": 0,
            "init_start_original": self._init_start,
            "init_end_original":   self._init_end,
        }
        fd, config_path = tempfile.mkstemp(suffix=".json", prefix="aiopslab_re2tt_")
        with os.fdopen(fd, "w") as f:
            json.dump(processing_config, f, indent=2)
        self._processing_config_path = config_path

        env = self._get_docker_env()

        # Bring down previous container (if any), then start fresh
        self.docker.compose_down(cwd=str(self.docker_deploy_path), env=env,
                                 project_name=self.namespace)
        self.docker.compose_up(cwd=str(self.docker_deploy_path), env=env, build=True,
                               project_name=self.namespace)

        container = self.get_container_name()

        print(f"  Loading RE2-TT telemetry: {container}")
        result = self.docker.exec_in_container(
            container,
            "python /app/process_telemetry_re2tt.py --mode init",
            timeout=300,
        )
        if result:
            print(result)

        print(f"  RE2-TT deployment complete.")

    def delete(self):
        try:
            self.docker.compose_down(
                cwd=str(self.docker_deploy_path),
                env=self._get_docker_env(),
                project_name=self.namespace,
            )
        except Exception:
            pass
        self._cleanup_config()

    def cleanup(self):
        try:
            self.docker.compose_down(
                cwd=str(self.docker_deploy_path),
                env=self._get_docker_env(),
                project_name=self.namespace,
            )
            self.docker.cleanup()
        except Exception:
            pass
        self._cleanup_config()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_docker_env(self):
        env = os.environ.copy()
        env["NAMESPACE"] = self.namespace
        env["DATASET_PATH"] = str(self.dataset_path)
        if self._processing_config_path:
            env["PROCESSING_CONFIG"] = self._processing_config_path
        return env

    def _cleanup_config(self):
        if self._processing_config_path and os.path.exists(self._processing_config_path):
            os.unlink(self._processing_config_path)
            self._processing_config_path = None
