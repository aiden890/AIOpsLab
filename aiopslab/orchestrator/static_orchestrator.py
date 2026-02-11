"""Orchestrator class for static dataset problems.

No K8s, no OpenEBS, no Prometheus. Uses Docker Compose for data containers only.
"""

from aiopslab.orchestrator.base import BaseOrchestrator
from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry


class StaticOrchestrator(BaseOrchestrator):
    """Orchestrator for static dataset problems (Docker-based)."""

    def __init__(self, results_dir=None):
        super().__init__(results_dir=results_dir)
        self.probs = StaticProblemRegistry()

    def _setup_environment(self, prob, deployment):
        """No K8s setup needed for static datasets."""
        pass

    def _teardown_environment(self, prob):
        """No K8s teardown needed for static datasets."""
        pass
