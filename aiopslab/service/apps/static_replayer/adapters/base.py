# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Base Dataset Adapter

Abstract base class for dataset-specific adapters.
Each adapter is responsible for:
1. Loading telemetry data (traces, logs, metrics) in standardized format
2. Providing a dataset-specific query parser
"""

from abc import ABC, abstractmethod
from pathlib import Path
import pandas as pd
from typing import Iterator, Dict, List


class BaseDatasetAdapter(ABC):
    """Base class for all dataset adapters"""

    def __init__(self, config: Dict):
        """
        Initialize dataset adapter

        Args:
            config: Full configuration dictionary for this dataset
        """
        self.config = config
        self.dataset_path = Path(config["dataset_path"])
        self.data_mapping = config.get("data_mapping", {})

    @abstractmethod
    def load_traces(self) -> Iterator[pd.DataFrame]:
        """
        Load trace data in standardized format

        Yields:
            pd.DataFrame: Each DataFrame contains trace spans with columns:
                - timestamp: float (Unix timestamp)
                - trace_id: str
                - span_id: str
                - parent_span_id: str
                - service: str (service name)
                - operation_name: str
                - duration: float (milliseconds)
                - tags: dict (additional key-value tags)
        """
        pass

    @abstractmethod
    def load_logs(self) -> Iterator[pd.DataFrame]:
        """
        Load log data in standardized format

        Yields:
            pd.DataFrame: Each DataFrame contains log entries with columns:
                - timestamp: float (Unix timestamp)
                - log_id: str
                - cmdb_id: str (component/service ID)
                - log_level: str (INFO, ERROR, WARN, etc.)
                - message: str (log message)
                - tags: dict (additional key-value tags)
        """
        pass

    @abstractmethod
    def load_metrics(self) -> Iterator[pd.DataFrame]:
        """
        Load metric data in standardized format

        Yields:
            pd.DataFrame: Each DataFrame contains metrics with columns:
                - timestamp: float (Unix timestamp)
                - metric_name: str
                - value: float
                - labels: dict (metric labels: cmdb_id, service, etc.)
        """
        pass

    @abstractmethod
    def get_query_parser(self):
        """
        Get dataset-specific query parser

        Returns:
            BaseQueryParser: Query parser instance for this dataset

        Example:
            from ..time_mapping.openrca_query_parser import OpenRCAQueryParser
            return OpenRCAQueryParser(self.dataset_path, self.config.get('query', {}))
        """
        pass

    def get_date_folders(self) -> List[Path]:
        """
        Get list of date folders (if dataset is organized by date)

        Returns:
            List of Path objects to date-specific folders

        Note:
            Override this method if your dataset has a different structure
        """
        telemetry_path = self.dataset_path / "telemetry"
        if telemetry_path.exists():
            return sorted([f for f in telemetry_path.iterdir() if f.is_dir()])
        return []
