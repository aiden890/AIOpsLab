# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
OpenRCA Static Dataset Detection Problems

Detection tasks using OpenRCA static datasets (Bank, Telecom, Market).
Uses StaticReplayer to replay historical data with known faults.
"""

from typing import Any
from aiopslab.orchestrator.tasks import DetectionTask
from aiopslab.service.apps.static_replayer import StaticReplayer
from aiopslab.session import SessionItem


class OpenRCADetectionBase:
    """Base class for OpenRCA detection problems"""

    def __init__(self, dataset_config: str):
        """
        Initialize OpenRCA detection problem

        Args:
            dataset_config: Config name (e.g., "openrca_bank")
        """
        self.replayer = StaticReplayer(dataset_config)
        self.app = self.replayer  # For compatibility with orchestrator
        self.namespace = self.replayer.namespace
        self.dataset_config = dataset_config

        # Get query info for expected solution
        if self.replayer.query_info:
            self.query_info = self.replayer.query_info
            self.expected_faults = self.query_info.faults
        else:
            self.query_info = None
            self.expected_faults = []

    def start_workload(self):
        """No workload needed - data is replayed from static dataset"""
        print("== No workload needed (using static dataset) ==")
        pass

    def inject_fault(self):
        """
        No fault injection needed - faults are already in the dataset
        Just start the replayer which will replay historical faults
        """
        print("== Starting Static Replayer ==")
        self.replayer.deploy()
        print(f"Dataset: {self.replayer.dataset_config['dataset_name']}")
        print(f"Namespace: {self.namespace}")

        if self.query_info:
            print(f"\nExpected Faults (from record.csv):")
            for fault in self.expected_faults:
                print(f"  - {fault['component']}: {fault['reason']} at {fault['datetime']}")
        print()

    def recover_fault(self):
        """Cleanup replayer"""
        print("== Cleaning up Static Replayer ==")
        self.replayer.cleanup()


class OpenRCABankDetection(OpenRCADetectionBase, DetectionTask):
    """OpenRCA Bank dataset detection task"""

    def __init__(self):
        OpenRCADetectionBase.__init__(self, "openrca_bank")
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        """
        Evaluate detection solution

        Args:
            soln: Agent's answer (should be "Yes" or "No")
            trace: Session trace
            duration: Time taken
        """
        print("== Evaluation ==")

        # OpenRCA datasets always have faults (from record.csv)
        expected_solution = "Yes"

        if isinstance(soln, str):
            if soln.strip().lower() == expected_solution.lower():
                print(f"✓ Correct detection: {soln}")
                self.add_result("Detection Accuracy", "Correct")
                self.results["success"] = True
            else:
                print(f"✗ Incorrect detection: {soln} (expected: {expected_solution})")
                self.add_result("Detection Accuracy", "Incorrect")
                self.results["success"] = False
        else:
            print("✗ Invalid solution format")
            self.add_result("Detection Accuracy", "Invalid Format")
            self.results["success"] = False

        # Add dataset-specific info to results
        self.add_result("Dataset", self.replayer.dataset_config['dataset_name'])
        self.add_result("Task ID", self.query_info.task_id if self.query_info else "N/A")
        self.add_result("Expected Faults", len(self.expected_faults))

        return super().eval(soln, trace, duration)


class OpenRCATelecomDetection(OpenRCADetectionBase, DetectionTask):
    """OpenRCA Telecom dataset detection task"""

    def __init__(self):
        OpenRCADetectionBase.__init__(self, "openrca_telecom")
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        print("== Evaluation ==")
        expected_solution = "Yes"

        if isinstance(soln, str):
            if soln.strip().lower() == expected_solution.lower():
                print(f"✓ Correct detection: {soln}")
                self.add_result("Detection Accuracy", "Correct")
                self.results["success"] = True
            else:
                print(f"✗ Incorrect detection: {soln}")
                self.add_result("Detection Accuracy", "Incorrect")
                self.results["success"] = False
        else:
            print("✗ Invalid solution format")
            self.add_result("Detection Accuracy", "Invalid Format")
            self.results["success"] = False

        self.add_result("Dataset", self.replayer.dataset_config['dataset_name'])
        self.add_result("Task ID", self.query_info.task_id if self.query_info else "N/A")

        return super().eval(soln, trace, duration)


class OpenRCAMarketCloudbed1Detection(OpenRCADetectionBase, DetectionTask):
    """OpenRCA Market Cloudbed-1 dataset detection task"""

    def __init__(self):
        OpenRCADetectionBase.__init__(self, "openrca_market_cloudbed1")
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        print("== Evaluation ==")
        expected_solution = "Yes"

        if isinstance(soln, str):
            if soln.strip().lower() == expected_solution.lower():
                print(f"✓ Correct detection: {soln}")
                self.add_result("Detection Accuracy", "Correct")
                self.results["success"] = True
            else:
                print(f"✗ Incorrect detection: {soln}")
                self.add_result("Detection Accuracy", "Incorrect")
                self.results["success"] = False
        else:
            print("✗ Invalid solution format")
            self.add_result("Detection Accuracy", "Invalid Format")
            self.results["success"] = False

        self.add_result("Dataset", self.replayer.dataset_config['dataset_name'])

        return super().eval(soln, trace, duration)


class OpenRCAMarketCloudbed2Detection(OpenRCADetectionBase, DetectionTask):
    """OpenRCA Market Cloudbed-2 dataset detection task"""

    def __init__(self):
        OpenRCADetectionBase.__init__(self, "openrca_market_cloudbed2")
        DetectionTask.__init__(self, self.app)

    def eval(self, soln: Any, trace: list[SessionItem], duration: float):
        print("== Evaluation ==")
        expected_solution = "Yes"

        if isinstance(soln, str):
            if soln.strip().lower() == expected_solution.lower():
                print(f"✓ Correct detection: {soln}")
                self.add_result("Detection Accuracy", "Correct")
                self.results["success"] = True
            else:
                print(f"✗ Incorrect detection: {soln}")
                self.add_result("Detection Accuracy", "Incorrect")
                self.results["success"] = False
        else:
            print("✗ Invalid solution format")
            self.add_result("Detection Accuracy", "Invalid Format")
            self.results["success"] = False

        self.add_result("Dataset", self.replayer.dataset_config['dataset_name'])

        return super().eval(soln, trace, duration)
