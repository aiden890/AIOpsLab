"""Orchestrator class for static dataset problems.

No K8s, no OpenEBS, no Prometheus. Uses Docker Compose for data containers only.
"""

import os
import shutil

from aiopslab.orchestrator.base import BaseOrchestrator
from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry

STATIC_OUTPUT_DIRS = [
    "static_logs_output",
    "static_metrics_output",
    "static_traces_output",
]


class StaticOrchestrator(BaseOrchestrator):
    """Orchestrator for static dataset problems (Docker-based)."""

    def __init__(self, results_dir=None):
        super().__init__(results_dir=results_dir)
        self.probs = StaticProblemRegistry()

    def _setup_environment(self, prob, deployment):
        """No K8s setup needed for static datasets."""
        pass

    def _teardown_environment(self, prob):
        """Clean up temporary output directories created by get_* actions."""
        for dirname in STATIC_OUTPUT_DIRS:
            dirpath = os.path.join(os.getcwd(), dirname)
            if os.path.isdir(dirpath):
                shutil.rmtree(dirpath)
                print(f"Cleaned up: {dirpath}")
