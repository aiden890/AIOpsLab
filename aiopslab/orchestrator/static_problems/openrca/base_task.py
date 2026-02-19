"""Base task for OpenRCA static dataset problems.

Provides common logic: app creation, query loading, no-op workload/fault.
"""

import pandas as pd
from pathlib import Path

from aiopslab.service.apps.static_dataset import StaticDataset
from aiopslab.orchestrator.static_actions.rca import StaticRCAActions


class OpenRCABaseTask:
    """Base class for all OpenRCA static dataset problems."""

    def __init__(self, config_name: str, query_index: int):
        """
        Args:
            config_name: Dataset config name (e.g., "openrca_bank").
            query_index: Row index in query.csv (0-based).
        """
        self.app = StaticDataset(config_name, query_index=query_index)
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

        # Set up actions that read from the Docker container
        self._actions = StaticRCAActions(
            container_name=self.app.get_container_name(),
            possible_root_causes=self.app.dataset_config.get("possible_root_causes"),
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
