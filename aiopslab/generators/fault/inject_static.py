"""Static fault injector for OpenRCA dataset.

Instead of injecting live faults, this selects a pre-recorded fault scenario
from record.csv. The ground truth is stored for evaluation.
"""

from aiopslab.generators.fault.base import FaultInjector
from aiopslab.service.apps.openrca.preprocess import load_records, get_fault_info


class StaticFaultInjector(FaultInjector):
    """Fault injector that selects pre-recorded faults from OpenRCA dataset."""

    def __init__(self, dataset_path: str, scenario_id: int, dataset_type: str = "market"):
        self.dataset_path = dataset_path
        self.scenario_id = scenario_id
        self.dataset_type = dataset_type
        self.records = load_records(dataset_path)
        self.active_fault = None

    def inject_fault(self, **kwargs):
        """Select the pre-recorded fault scenario."""
        record = self.records.iloc[self.scenario_id]
        self.active_fault = get_fault_info(record, self.dataset_type)
        print(f"[StaticFaultInjector] Selected scenario {self.scenario_id}:")
        print(f"  Component: {self.active_fault['component']}")
        print(f"  Reason: {self.active_fault['reason']}")
        print(f"  Datetime: {self.active_fault['datetime']}")

    def recover_fault(self):
        """No-op recovery for static faults."""
        self.active_fault = None

    def get_ground_truth(self) -> dict:
        """Return the ground truth for evaluation.

        Returns dict with original dataset fields:
            component, reason, datetime, level, faulty_service
        """
        if self.active_fault is None:
            raise RuntimeError("No fault is active. Call inject_fault() first.")
        return self.active_fault
