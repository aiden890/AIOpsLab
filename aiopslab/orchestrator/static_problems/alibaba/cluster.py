"""Alibaba Cluster dataset problem definitions.

Alibaba dataset has metrics only (no logs, no traces).
"""

from typing import Any
from aiopslab.orchestrator.tasks import *
from aiopslab.service.apps.static_dataset import StaticDataset
from aiopslab.session import SessionItem


class AlibabaClusterBaseTask:
    def __init__(self):
        self.app = StaticDataset("alibaba_cluster")
        self.namespace = self.app.namespace

    def start_workload(self):
        pass

    def inject_fault(self):
        pass

    def recover_fault(self):
        pass


class AlibabaClusterDetection(AlibabaClusterBaseTask, DetectionTask):
    def __init__(self):
        AlibabaClusterBaseTask.__init__(self)
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        expected = "Yes"
        if isinstance(soln, str) and soln.strip().lower() == expected.lower():
            self.add_result("Detection Accuracy", "Correct")
        else:
            self.add_result("Detection Accuracy", "Incorrect")
        return super().eval(soln, trace, duration)
