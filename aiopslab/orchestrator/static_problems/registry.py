"""Static problem registry for OpenRCA datasets.

Separate from the main ProblemRegistry in orchestrator/problems/.
Dynamically generates problem entries for all OpenRCA datasets.
"""

from aiopslab.paths import (
    OPENRCA_CLOUDBED1_METADATA, OPENRCA_CLOUDBED2_METADATA,
    OPENRCA_TELECOM_METADATA, OPENRCA_BANK_METADATA,
    OPENRCA_DATASET_DIR,
)
from aiopslab.orchestrator.static_problems.openrca_problems import (
    OpenRCAStaticDetection,
    OpenRCAStaticLocalization,
    OpenRCAStaticAnalysis,
)
from aiopslab.service.apps.openrca.preprocess import get_available_scenarios


# Dataset configurations
OPENRCA_DATASETS = [
    ("openrca_cb1", OPENRCA_CLOUDBED1_METADATA, "market", "Market/cloudbed-1"),
    ("openrca_cb2", OPENRCA_CLOUDBED2_METADATA, "market", "Market/cloudbed-2"),
    ("openrca_telecom", OPENRCA_TELECOM_METADATA, "telecom", "Telecom"),
    ("openrca_bank", OPENRCA_BANK_METADATA, "bank", "Bank"),
]

TASK_CLASSES = {
    "detection": OpenRCAStaticDetection,
    "localization": OpenRCAStaticLocalization,
    "analysis": OpenRCAStaticAnalysis,
}


class StaticProblemRegistry:
    """Registry for OpenRCA static replay problems.

    Problem ID format: {prefix}-{task_type}-{scenario_id}
    Examples:
        openrca_cb1-detection-0
        openrca_cb1-localization-0
        openrca_cb1-analysis-0
        openrca_telecom-detection-5
        openrca_bank-analysis-12

    Usage:
        registry = StaticProblemRegistry()
        problem = registry.get_problem_instance("openrca_cb1-detection-0")
    """

    def __init__(self):
        self.PROBLEM_REGISTRY = {}
        self._register_all()

    def _register_all(self):
        """Register all OpenRCA static problems."""
        for prefix, metadata_path, dataset_type, dataset_subpath in OPENRCA_DATASETS:
            dataset_path = str(OPENRCA_DATASET_DIR / dataset_subpath)
            config_file = str(metadata_path)

            try:
                scenarios = get_available_scenarios(dataset_path, dataset_type)
            except FileNotFoundError:
                # Dataset not downloaded, skip
                continue

            for scenario_id in scenarios:
                for task_name, task_cls in TASK_CLASSES.items():
                    problem_id = f"{prefix}-{task_name}-{scenario_id}"
                    _cf = config_file
                    _sid = scenario_id
                    _dt = dataset_type
                    _cls = task_cls
                    self.PROBLEM_REGISTRY[problem_id] = (
                        lambda cf=_cf, sid=_sid, dt=_dt, cls=_cls:
                            cls(config_file=cf, scenario_id=sid, dataset_type=dt)
                    )

    def get_problem_instance(self, problem_id: str):
        if problem_id not in self.PROBLEM_REGISTRY:
            raise ValueError(f"Problem ID {problem_id} not found in static registry.")
        return self.PROBLEM_REGISTRY[problem_id]()

    def get_problem(self, problem_id: str):
        return self.PROBLEM_REGISTRY.get(problem_id)

    def get_problem_ids(self, task_type: str = None, dataset: str = None):
        """Get problem IDs, optionally filtered by task type and/or dataset.

        Args:
            task_type: Filter by task type ("detection", "localization", "analysis")
            dataset: Filter by dataset prefix ("openrca_cb1", "openrca_telecom", etc.)
        """
        ids = list(self.PROBLEM_REGISTRY.keys())
        if task_type:
            ids = [k for k in ids if task_type in k]
        if dataset:
            ids = [k for k in ids if k.startswith(dataset)]
        return ids

    def get_problem_count(self, task_type: str = None, dataset: str = None):
        return len(self.get_problem_ids(task_type, dataset))

    def has_problem(self, problem_id: str) -> bool:
        return problem_id in self.PROBLEM_REGISTRY

    def get_problem_deployment(self, problem_id: str):
        """All static problems use k8s deployment."""
        return "k8s"
