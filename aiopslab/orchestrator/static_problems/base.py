# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Base class for static dataset problems."""

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from aiopslab.session import SessionItem

logger = logging.getLogger(__name__)


class BaseStaticProblem(ABC):
    """Base class for static dataset problems."""

    def __init__(self):
        """Initialize static problem."""
        self.app: Optional[Any] = None  # Static replayer app
        self.actions: Optional[Any] = None  # StaticActions instance
        self.results: Dict[str, Any] = {}
        self.ground_truth: Optional[Dict[str, Any]] = None

    @abstractmethod
    def load_ground_truth(self):
        """Load ground truth from dataset (e.g., record.csv)."""
        pass

    @abstractmethod
    def get_task_description(self) -> str:
        """Return task description for the agent."""
        pass

    @abstractmethod
    def get_instructions(self) -> str:
        """Return detailed instructions for the agent."""
        pass

    def get_available_actions(self) -> List[str]:
        """Return list of available action names."""
        return [
            'query_static_traces',
            'query_static_logs',
            'query_static_metrics',
            'get_available_services',
            'submit_solution'
        ]

    def start_replayer(self, timeout: int = 300):
        """
        Start the replayer Docker container.

        Args:
            timeout: Maximum wait time in seconds (default: 300)
        """
        logger.info(f"Starting replayer for {self.app.config['dataset']['namespace']}...")

        # Use app's start_and_wait which waits for .phase1_complete marker
        self.app.start_and_wait(timeout=timeout)

        # Initialize static actions with observer APIs
        from aiopslab.orchestrator.actions.static_actions import StaticActions
        observer_apis = self.app.get_telemetry_apis()
        self.actions = StaticActions(observer_apis)

        logger.info("Replayer ready. Telemetry data available.")

    def stop_replayer(self):
        """Stop the replayer Docker container."""
        self.app.stop_replayer()

    def _wait_for_replayer_ready(self, timeout: int = 300):
        """
        Wait for replayer to finish loading history.

        Args:
            timeout: Maximum wait time in seconds

        Raises:
            TimeoutError: If replayer doesn't become ready in time
        """
        logger.info("Waiting for replayer to finish bulk loading...")

        start = time.time()
        while time.time() - start < timeout:
            if self._check_telemetry_ready():
                logger.info(f"Replayer ready after {time.time()-start:.1f}s")
                return
            time.sleep(5)

        raise TimeoutError(
            f"Replayer did not become ready within {timeout}s. "
            f"Check Docker logs for details."
        )

    def _check_telemetry_ready(self) -> bool:
        """Check if telemetry CSV files are ready."""
        output_path = self.app.get_output_path()

        # Check for at least one telemetry type
        enabled_types = self.app.config.get('telemetry', {}).get('enabled', [])

        for telemetry_type in enabled_types:
            csv_file = output_path / f"{telemetry_type}.csv"
            if csv_file.exists() and csv_file.stat().st_size > 0:
                return True

        return False

    def perform_action(self, action_name: str, *args, **kwargs) -> Any:
        """
        Execute an action.

        Args:
            action_name: Name of action to execute
            *args: Positional arguments for action
            **kwargs: Keyword arguments for action

        Returns:
            Action result
        """
        if action_name == 'submit_solution':
            # Special handling for solution submission
            return kwargs.get('solution') or (args[0] if args else None)

        if not hasattr(self.actions, action_name):
            available = ', '.join(self.get_available_actions())
            raise ValueError(
                f"Unknown action: {action_name}. "
                f"Available actions: {available}"
            )

        action_func = getattr(self.actions, action_name)
        return action_func(*args, **kwargs)

    @abstractmethod
    def eval(
        self,
        soln: Any,
        trace: List[SessionItem],
        duration: float
    ) -> Dict[str, Any]:
        """
        Evaluate the solution.

        Args:
            soln: Agent's solution
            trace: Session trace
            duration: Execution duration in seconds

        Returns:
            Evaluation results dictionary
        """
        pass

    def add_result(self, key: str, value: Any):
        """Add evaluation result."""
        self.results[key] = value
