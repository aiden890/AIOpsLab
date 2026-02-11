"""Registry for static dataset problems.

Dynamically generates problem entries from query.csv files.
Each row in query.csv becomes an independent problem instance.
"""

import pandas as pd
from pathlib import Path

from aiopslab.paths import BASE_PARENT_DIR

from aiopslab.orchestrator.static_problems.openrca import (
    OpenRCABankProblem,
    OpenRCATelecomProblem,
    OpenRCAMarketCB1Problem,
    OpenRCAMarketCB2Problem,
)

# Dataset configurations: config_key -> (relative dataset path, problem class)
OPENRCA_DATASETS = {
    "openrca_bank": (
        "aiopslab-applications/static_dataset/openrca/Bank",
        OpenRCABankProblem,
    ),
    "openrca_telecom": (
        "aiopslab-applications/static_dataset/openrca/Telecom",
        OpenRCATelecomProblem,
    ),
    "openrca_market_cb1": (
        "aiopslab-applications/static_dataset/openrca/Market/cloudbed-1",
        OpenRCAMarketCB1Problem,
    ),
    "openrca_market_cb2": (
        "aiopslab-applications/static_dataset/openrca/Market/cloudbed-2",
        OpenRCAMarketCB2Problem,
    ),
}


class StaticProblemRegistry:
    def __init__(self):
        self.PROBLEM_REGISTRY = {}
        self._load_openrca_problems()
        # All static problems use Docker
        self.DOCKER_REGISTRY = list(self.PROBLEM_REGISTRY.keys())

    def _load_openrca_problems(self):
        """Dynamically generate problem entries from query.csv files."""
        for ds_key, (rel_path, cls) in OPENRCA_DATASETS.items():
            query_csv = BASE_PARENT_DIR / rel_path / "query.csv"
            if not query_csv.exists():
                print(f"Warning: query.csv not found at {query_csv}")
                continue

            df = pd.read_csv(query_csv)
            for idx in range(len(df)):
                task_type = df.iloc[idx]["task_index"]
                pid = f"{ds_key}-{task_type}-{idx}"
                self.PROBLEM_REGISTRY[pid] = (cls, idx)

    def get_problem_instance(self, problem_id: str):
        if problem_id not in self.PROBLEM_REGISTRY:
            raise ValueError(f"Problem ID '{problem_id}' not found in static registry.")
        cls, query_index = self.PROBLEM_REGISTRY[problem_id]
        return cls(query_index=query_index)

    def get_problem_ids(self, task_type: str = None, dataset: str = None):
        ids = list(self.PROBLEM_REGISTRY.keys())
        if task_type:
            ids = [pid for pid in ids if task_type in pid]
        if dataset:
            ids = [pid for pid in ids if pid.startswith(dataset)]
        return ids

    def get_problem_count(self, task_type: str = None, dataset: str = None):
        return len(self.get_problem_ids(task_type=task_type, dataset=dataset))

    def get_problem_deployment(self, problem_id: str):
        return "docker"
