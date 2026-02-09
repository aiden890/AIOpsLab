# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Base Query Parser

Abstract base class for dataset-specific query parsers.
Each dataset can have its own query format, and the parser
converts it to a standardized QueryResult object.
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, List
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class QueryResult:
    """Standardized query parsing result"""

    task_id: str
    """Task identifier (e.g., 'task_1', 'job_123', 'scenario_abc')"""

    time_range: Dict[str, any]
    """
    Time range dictionary with keys:
    - start: int (Unix timestamp)
    - end: int (Unix timestamp)
    - start_str: str (human-readable datetime)
    - end_str: str (human-readable datetime)
    - duration: int (seconds)
    """

    faults: List[Dict[str, any]]
    """
    List of fault/incident dictionaries, each with:
    - timestamp: int (Unix timestamp)
    - datetime: str (human-readable datetime)
    - level: str (e.g., 'pod', 'service', 'node')
    - component: str (component/service name)
    - reason: str (fault description)
    """

    metadata: Optional[Dict[str, any]] = field(default_factory=dict)
    """Dataset-specific additional information"""

    def __repr__(self):
        return (
            f"QueryResult(task={self.task_id}, "
            f"range={self.time_range['start_str']}~{self.time_range['end_str']}, "
            f"faults={len(self.faults)})"
        )

    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization"""
        return {
            'task_id': self.task_id,
            'time_range': self.time_range,
            'faults': self.faults,
            'metadata': self.metadata
        }


class BaseQueryParser(ABC):
    """Base class for all dataset-specific query parsers"""

    def __init__(self, dataset_path: Path, config: Dict):
        """
        Initialize query parser

        Args:
            dataset_path: Path to the dataset directory
            config: Configuration dictionary (usually from config['query'])
        """
        self.dataset_path = Path(dataset_path)
        self.config = config

    @abstractmethod
    def parse_task(self, task_identifier: str) -> QueryResult:
        """
        Parse a specific task/scenario

        Args:
            task_identifier: Task ID, job name, scenario ID, etc.
                            (format depends on dataset)

        Returns:
            QueryResult: Standardized query result

        Raises:
            ValueError: If task not found or invalid
            FileNotFoundError: If required files are missing
        """
        pass

    @abstractmethod
    def list_tasks(self) -> List[str]:
        """
        List all available tasks/scenarios in the dataset

        Returns:
            List of task identifiers
        """
        pass

    def validate_time_range(self, time_range: Dict) -> bool:
        """
        Validate time range dictionary

        Args:
            time_range: Time range dictionary to validate

        Returns:
            True if valid, False otherwise
        """
        required_keys = ['start', 'end', 'start_str', 'end_str', 'duration']
        return all(key in time_range for key in required_keys)

    def get_task_summary(self) -> str:
        """
        Get a summary of all available tasks

        Returns:
            Multi-line string with task information
        """
        tasks = self.list_tasks()
        summary = f"Available tasks in {self.dataset_path.name}:\n"
        summary += f"  Total: {len(tasks)}\n"
        summary += "  Tasks:\n"
        for i, task_id in enumerate(tasks[:10], 1):
            summary += f"    {i}. {task_id}\n"
        if len(tasks) > 10:
            summary += f"    ... and {len(tasks) - 10} more\n"
        return summary
