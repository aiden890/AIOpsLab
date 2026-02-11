"""OpenRCA Bank dataset problem definitions."""

from .base_task import OpenRCABaseTask
from aiopslab.orchestrator.tasks.openrca_task import OpenRCATask


class OpenRCABankProblem(OpenRCABaseTask, OpenRCATask):
    """Bank dataset problem. Selected by query_index from query.csv."""

    def __init__(self, query_index: int):
        OpenRCABaseTask.__init__(self, "openrca_bank", query_index)
        OpenRCATask.__init__(self, self.app, self.query_row, self.task_type)
        self.actions = self._actions
