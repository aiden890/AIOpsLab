# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Orchestrator class for K8s-based problems."""

from aiopslab.orchestrator.base import BaseOrchestrator
from aiopslab.service.kubectl import KubeCtl
from aiopslab.orchestrator.problems.registry import ProblemRegistry
from aiopslab.service.telemetry.prometheus import Prometheus


class Orchestrator(BaseOrchestrator):
    """Orchestrator for Kubernetes-based problems (original behavior)."""

    def __init__(self, results_dir=None):
        super().__init__(results_dir=results_dir)
        self.probs = ProblemRegistry()
        self.kubectl = KubeCtl()
        self.prometheus = None

    def _setup_environment(self, prob, deployment):
        """Setup K8s environment: OpenEBS + Prometheus."""
        if deployment != "docker":
            print("Setting up OpenEBS...")
            self.kubectl.exec_command(
                "kubectl apply -f https://openebs.github.io/charts/openebs-operator.yaml"
            )
            self.kubectl.exec_command(
                "kubectl patch storageclass openebs-hostpath -p "
                "'{\"metadata\": {\"annotations\":{\"storageclass.kubernetes.io/is-default-class\":\"true\"}}}'"
            )
            self.kubectl.wait_for_ready("openebs")
            print("OpenEBS setup completed.")

            self.prometheus = Prometheus()
            self.prometheus.deploy()

    def _teardown_environment(self, prob):
        """Teardown K8s environment: Prometheus + OpenEBS."""
        if prob.namespace != "docker" and self.prometheus:
            self.prometheus.teardown()
            print("Uninstalling OpenEBS...")
            self.kubectl.exec_command(
                "kubectl delete sc openebs-hostpath openebs-device --ignore-not-found"
            )
            self.kubectl.exec_command(
                "kubectl delete -f https://openebs.github.io/charts/openebs-operator.yaml"
            )
            self.kubectl.wait_for_namespace_deletion("openebs")
