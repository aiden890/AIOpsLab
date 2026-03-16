"""RE2-TT Root Cause Analysis problem definition.

Each problem corresponds to one row in query.csv (one fault injection case).
Uses the static_dataset Docker infrastructure with process_telemetry_re2tt.py.
Evaluation reuses the openrca_eval scoring logic (scoring_points string matching).
"""

import inspect
import pandas as pd
from pathlib import Path

from aiopslab.service.apps.static_dataset import RE2TTDataset, FAULT_REASON_MAP
from aiopslab.orchestrator.static_actions.rca import StaticRCAActions
from aiopslab.orchestrator.tasks.openrca_task import OpenRCATask
from aiopslab.paths import BASE_PARENT_DIR


# Default path to RE2-TT dataset (relative to AIOpsLab project root)
_RE2TT_DEFAULT_ROOT = BASE_PARENT_DIR.parent / "RCAEval" / "data" / "RE2" / "RE2-TT"

_QUERY_CSV = Path(__file__).parent / "query.csv"


def _get_services_from_metrics(case_path: Path) -> list:
    """Extract unique service names from simple_metrics.csv column headers.

    Column format: {service}_{metric_type}  (e.g., ts-auth-service_cpu)
    Service names are everything before the last _ followed by a known metric type.
    """
    metrics_file = case_path / "simple_metrics.csv"
    if not metrics_file.exists():
        return []

    # Read header only
    header = pd.read_csv(metrics_file, nrows=0).columns.tolist()

    # Known metric suffixes in RE2-TT simple_metrics.csv
    metric_suffixes = {"cpu", "mem", "latency", "errors", "diskio", "socket",
                       "network-receive-bytes-total", "network-transmit-bytes-total"}

    services = set()
    for col in header:
        if col == "time":
            continue
        # Split on last "_" — RE2-TT columns are {service}_{type}
        # but type can also be "latency-50", "latency-90" etc.
        parts = col.rsplit("_", 1)
        if len(parts) == 2:
            svc, metric = parts[0], parts[1]
            # Accept the service if metric matches any known type or starts with known type
            base_metric = metric.split("-")[0]
            if base_metric in metric_suffixes or metric in metric_suffixes:
                services.add(svc)

    return sorted(services)


def _build_namespace(query_index: int) -> str:
    """Build an opaque Docker namespace that reveals no RCA hints.

    e.g., query_index=0 → re2tt-case-0000
    """
    return f"re2tt-case-{query_index:04d}"


class RE2TTProblem(OpenRCATask):
    """One RE2-TT RCA problem (one fault injection case).

    Inherits OpenRCATask for task description, action dispatch, and eval.
    """

    def __init__(self, query_index: int, work_dir: str = None,
                 re2tt_root: Path = None, condition: str = None):
        """
        Args:
            query_index: Row index in query.csv (0-based).
            work_dir:    Directory for saving telemetry CSV outputs.
            re2tt_root:  Path to RE2-TT root directory. Defaults to the standard location.
            condition:   Telemetry ablation condition (e.g., "no_log"). Adjusts enabled
                         telemetry flags for the actions object.
        """
        re2tt_root = Path(re2tt_root) if re2tt_root else _RE2TT_DEFAULT_ROOT

        # Load query row
        query_df = pd.read_csv(_QUERY_CSV)
        if query_index >= len(query_df):
            raise IndexError(f"query_index {query_index} out of range (max {len(query_df)-1})")

        row = query_df.iloc[query_index]
        service    = row["service"]
        fault      = row["fault"]
        trial      = str(row["trial"])
        inject_time = int(row["inject_time"])
        case_dir   = row["case_dir"]
        case_path  = re2tt_root / case_dir

        namespace = _build_namespace(query_index)

        # Apply ablation condition suffix
        self.condition = condition
        if condition and condition != "all":
            namespace = f"{namespace}-{condition.replace('_', '-')}"

        # 30-min aligned window: floor inject_time to nearest 30-min boundary
        _slot = 30 * 60
        window_start = (inject_time // _slot) * _slot
        window_end   = window_start + _slot

        # Extract services from metrics CSV
        services = _get_services_from_metrics(case_path)
        if not services:
            services = [service]  # fallback

        # Build the dataset app
        self.app = RE2TTDataset(
            case_path=case_path,
            namespace=namespace,
            inject_time=inject_time,
            services=services,
            fault_service=service,
            fault_type=fault,
            window_start=window_start,
            window_end=window_end,
        )

        # Apply ablation condition telemetry flags
        CONDITION_FLAGS = {
            "no_log":    {"enable_log": False, "enable_metric": True,  "enable_trace": True},
            "no_metric": {"enable_log": True,  "enable_metric": False, "enable_trace": True},
            "no_trace":  {"enable_log": True,  "enable_metric": True,  "enable_trace": False},
        }
        if condition in CONDITION_FLAGS:
            self.app.dataset_config["telemetry"] = CONDITION_FLAGS[condition]

        telemetry_flags = self.app.dataset_config.get("telemetry")

        # Build actions
        self._actions = StaticRCAActions(
            container_name=self.app.get_container_name(),
            possible_root_causes=self.app.dataset_config.get("possible_root_causes"),
            telemetry_flags=telemetry_flags,
            use_executor=True,
            work_dir=work_dir,
        )

        # query_row matches the OpenRCATask expected format
        query_row = {
            "task_index":     row["task_index"],
            "instruction":    row["instruction"],
            "scoring_points": row["scoring_points"],
        }

        # Initialize OpenRCATask (provides task_desc, eval, action dispatch)
        OpenRCATask.__init__(self, self.app, query_row, task_type="task_rca")
        self.actions = self._actions
        self.namespace = namespace

    # ------------------------------------------------------------------
    # Lifecycle stubs (static dataset — no real workload/fault injection)
    # ------------------------------------------------------------------

    def start_workload(self):
        pass

    def inject_fault(self):
        """Deploying the container IS the fault injection for static datasets."""
        pass

    def recover_fault(self):
        pass
