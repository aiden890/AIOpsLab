"""OpenRCA Market Cloudbed-1 dataset problem definitions."""

from .base_task import OpenRCABaseTask
from aiopslab.orchestrator.tasks.openrca_task import OpenRCATask


class OpenRCAMarketCB1Problem(OpenRCABaseTask, OpenRCATask):
    """Market Cloudbed-1 dataset problem. Selected by query_index from query.csv."""

    def __init__(self, query_index: int):
        OpenRCABaseTask.__init__(self, "openrca_market_cloudbed1", query_index)
        OpenRCATask.__init__(self, self.app, self.query_row, self.task_type)
        self.actions = self._actions
