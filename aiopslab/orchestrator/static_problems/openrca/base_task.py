"""Base task for OpenRCA static dataset problems.

Provides common logic: app creation, query loading, no-op workload/fault.
"""

import pandas as pd

from aiopslab.service.apps.static_dataset import StaticDataset
from aiopslab.orchestrator.static_actions.rca import StaticRCAActions


class OpenRCABaseTask:
    """Base class for all OpenRCA static dataset problems."""

    def __init__(self, config_name: str, query_index: int, work_dir: str = None,
                 condition: str = None):
        """
        Args:
            config_name: Dataset config name (e.g., "openrca_bank").
            query_index: Row index in query.csv (0-based).
            work_dir: Directory for saving telemetry CSV files.
                      Use unique paths for parallel runs to avoid conflicts.
            condition: Telemetry ablation condition for container isolation.
        """
        self.app = StaticDataset(config_name, query_index=query_index,
                                 condition=condition)
        self.namespace = self.app.namespace
        self.query_index = query_index

        # Use UTC-converted instruction/scoring_points from query_info (set by dataset.py)
        # Falls back to raw CSV if query_info is unavailable
        if self.app.query_info:
            meta = self.app.query_info.metadata
            self.query_row = {
                "task_index": self.app.query_info.task_id,
                "instruction": meta.get("instruction", ""),
                "scoring_points": meta.get("scoring_points", ""),
            }
        else:
            query_file = self.app.dataset_path / self.app.dataset_config.get(
                "query", {}
            ).get("query_file", "query.csv")
            query_df = pd.read_csv(query_file)
            self.query_row = query_df.iloc[query_index].to_dict()

        self.task_type = self.query_row["task_index"]

        # Default: callback-based actions (set_executor() injection).
        # Runners that need the self-contained executor (e.g. run_react_rca.py)
        # swap this out for StaticRCAActionsWithExecutor after deployment.
        executor_cfg = self.app.dataset_config.get("executor", {})
        default_start, default_end = self.app.get_default_time_window()
        self._actions = StaticRCAActions(
            raw_dataset_path=str(self.app.get_raw_dataset_path()),
            raw_data_mapping=self.app.dataset_config.get("data_mapping"),
            raw_dataset_type=self.app.dataset_config.get("dataset_type"),
            default_start_time=default_start,
            default_end_time=default_end,
            possible_root_causes=self.app.dataset_config.get("possible_root_causes"),
            telemetry_flags=self.app.dataset_config.get("telemetry"),
            use_executor=executor_cfg.get("enable", True),
            work_dir=work_dir,
        )

    def start_workload(self):
        """No workload for static datasets."""
        pass

    def inject_fault(self):
        """For static datasets, deploying the app IS the fault injection.
        The dataset already contains fault data.
        """
        pass

    def recover_fault(self):
        """Cleanup is handled by app.cleanup()."""
        pass
