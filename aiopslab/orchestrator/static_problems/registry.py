# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Registry for static dataset problems."""

import logging
import re
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class StaticProblemRegistry:
    """
    Registry for static dataset problems.

    Maps problem IDs to problem instances.

    Problem ID Format:
        {dataset}_{namespace}_{task}_{instance}

    Examples:
        - openrca_bank_task1_0 → OpenRCAProblem("Bank", "task_1", 0)
        - openrca_bank_task6_1 → OpenRCAProblem("Bank", "task_6", 1)
        - openrca_telecom_task1 → OpenRCAProblem("Telecom", "task_1", random)
    """

    def __init__(self):
        """Initialize registry."""
        self._problems: Dict[str, type] = {}
        self._register_default_problems()
        logger.info("StaticProblemRegistry initialized")

    def _register_default_problems(self):
        """Register default problem types."""
        try:
            # Register OpenRCA problems
            from aiopslab.orchestrator.static_problems.openrca import OpenRCAProblem
            self._problems['openrca'] = OpenRCAProblem
            logger.debug("Registered OpenRCA problems")
        except ImportError as e:
            logger.warning(f"Could not register OpenRCA problems: {e}")

        # Future: Register other datasets (Alibaba, ACME, etc.)
        # from aiopslab.orchestrator.static_problems.alibaba import AlibabaProblem
        # self._problems['alibaba'] = AlibabaProblem

    def get_problem_instance(self, problem_id: str) -> Any:
        """
        Get problem instance by ID.

        Args:
            problem_id: Problem identifier (e.g., 'openrca_bank_task1_0')

        Returns:
            Problem instance

        Raises:
            ValueError: If problem ID is invalid or dataset not supported

        Examples:
            >>> registry = StaticProblemRegistry()
            >>> problem = registry.get_problem_instance('openrca_bank_task1_0')
            >>> problem = registry.get_problem_instance('openrca_telecom_task6')
        """
        # Parse problem ID
        parsed = self._parse_problem_id(problem_id)
        dataset_type = parsed['dataset']
        namespace = parsed['namespace']
        task_index = parsed['task_index']
        instance = parsed['instance']

        logger.info(
            f"Creating problem: dataset={dataset_type}, namespace={namespace}, "
            f"task={task_index}, instance={instance}"
        )

        # Get problem class
        if dataset_type not in self._problems:
            available = ', '.join(self._problems.keys())
            raise ValueError(
                f"Dataset '{dataset_type}' not supported. "
                f"Available datasets: {available}"
            )

        problem_class = self._problems[dataset_type]

        # Create instance
        # For OpenRCA: OpenRCAProblem(dataset="Bank", task_index="task_1", instance=0)
        problem = problem_class(
            dataset=namespace.capitalize(),
            task_index=task_index,
            instance=instance
        )

        logger.info(f"✓ Created problem: {problem_id}")
        return problem

    def _parse_problem_id(self, problem_id: str) -> Dict[str, Any]:
        """
        Parse problem ID into components.

        Format: {dataset}_{namespace}_{task}_{instance}

        Args:
            problem_id: Problem ID string

        Returns:
            Dictionary with: dataset, namespace, task_index, instance

        Raises:
            ValueError: If problem ID format is invalid
        """
        # Pattern: openrca_bank_task1_0 or openrca_bank_task1
        # Parts: dataset, namespace, task_number, optional instance
        pattern = r'^([a-z]+)_([a-z]+)_task(\d+)(?:_(\d+))?$'
        match = re.match(pattern, problem_id.lower())

        if not match:
            raise ValueError(
                f"Invalid problem ID format: '{problem_id}'. "
                f"Expected format: dataset_namespace_taskN or dataset_namespace_taskN_instance "
                f"(e.g., 'openrca_bank_task1_0' or 'openrca_bank_task1')"
            )

        dataset = match.group(1)
        namespace = match.group(2)
        task_number = match.group(3)
        instance_str = match.group(4)

        # Convert to task_index format (task_1, task_6, etc.)
        task_index = f"task_{task_number}"

        # Parse instance (None if not specified)
        instance = int(instance_str) if instance_str else None

        return {
            'dataset': dataset,
            'namespace': namespace,
            'task_index': task_index,
            'instance': instance
        }

    def list_problems(self, dataset: Optional[str] = None) -> List[str]:
        """
        List available problem types.

        Args:
            dataset: Optional dataset filter (e.g., 'openrca')

        Returns:
            List of available datasets or problem patterns

        Examples:
            >>> registry.list_problems()
            ['openrca']
            >>> registry.list_problems('openrca')
            ['openrca_bank_task1', 'openrca_bank_task6', ...]
        """
        if dataset is None:
            # Return available datasets
            return list(self._problems.keys())

        if dataset not in self._problems:
            return []

        # For OpenRCA, we could enumerate all possible combinations
        # For now, return a template
        if dataset == 'openrca':
            return [
                f"{dataset}_bank_task1",
                f"{dataset}_bank_task6",
                f"{dataset}_telecom_task1",
                # Add more as needed
            ]

        return []

    def register_problem(self, dataset: str, problem_class: type):
        """
        Register a custom problem type.

        Args:
            dataset: Dataset identifier (e.g., 'custom')
            problem_class: Problem class to register
        """
        self._problems[dataset] = problem_class
        logger.info(f"Registered custom problem: {dataset}")

    def get_problem_info(self, problem_id: str) -> Dict[str, str]:
        """
        Get information about a problem without instantiating it.

        Args:
            problem_id: Problem identifier

        Returns:
            Dictionary with problem metadata
        """
        parsed = self._parse_problem_id(problem_id)

        return {
            'problem_id': problem_id,
            'dataset': parsed['dataset'],
            'namespace': parsed['namespace'],
            'task_index': parsed['task_index'],
            'instance': str(parsed['instance']) if parsed['instance'] is not None else 'auto',
            'problem_class': self._problems.get(parsed['dataset']).__name__ if parsed['dataset'] in self._problems else 'Unknown'
        }
