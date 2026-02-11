# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Base classes for static dataset observers."""

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime
from typing import Any, Dict
import pandas as pd

logger = logging.getLogger(__name__)


class StaticObserverBase(ABC):
    """Base class for static dataset observers."""

    def __init__(self, output_path: Path):
        """
        Initialize static observer.

        Args:
            output_path: Path to telemetry output directory
        """
        self.output_path = Path(output_path)

    @abstractmethod
    def extract_data(self, start_time: datetime, end_time: datetime, **filters) -> pd.DataFrame:
        """
        Extract data within time window with optional filters.

        Args:
            start_time: Start of time window
            end_time: End of time window
            **filters: Additional filters (implementation-specific)

        Returns:
            DataFrame with filtered data
        """
        pass

    @abstractmethod
    def get_csv_path(self) -> Path:
        """Return path to CSV file for this telemetry type."""
        pass

    def _read_csv_with_time_filter(
        self,
        start_time: datetime,
        end_time: datetime,
        timestamp_col: str = 'timestamp'
    ) -> pd.DataFrame:
        """
        Read CSV and filter by time window.

        Args:
            start_time: Start of time window
            end_time: End of time window
            timestamp_col: Name of timestamp column

        Returns:
            Filtered DataFrame
        """
        csv_path = self.get_csv_path()

        if not csv_path.exists():
            logger.warning(f"CSV file not found: {csv_path}")
            return pd.DataFrame()

        try:
            df = pd.read_csv(csv_path)

            if df.empty:
                logger.debug(f"CSV file is empty: {csv_path}")
                return df

            # Convert timestamp column to datetime
            if timestamp_col in df.columns:
                # Handle both Unix timestamps and datetime strings
                if pd.api.types.is_numeric_dtype(df[timestamp_col]):
                    df['datetime'] = pd.to_datetime(df[timestamp_col], unit='s')
                else:
                    df['datetime'] = pd.to_datetime(df[timestamp_col])

                # Convert input times to naive UTC for comparison
                # (CSV datetimes are naive, so we need to match)
                if hasattr(start_time, 'tzinfo') and start_time.tzinfo is not None:
                    start_time = start_time.replace(tzinfo=None)
                if hasattr(end_time, 'tzinfo') and end_time.tzinfo is not None:
                    end_time = end_time.replace(tzinfo=None)

                # Filter by time window
                mask = (df['datetime'] >= start_time) & (df['datetime'] <= end_time)
                filtered = df[mask]

                logger.debug(f"Read {len(df)} rows, filtered to {len(filtered)} rows")
                return filtered
            else:
                logger.error(f"Timestamp column '{timestamp_col}' not found in {csv_path}")
                return pd.DataFrame()

        except Exception as e:
            logger.error(f"Error reading CSV {csv_path}: {e}", exc_info=True)
            return pd.DataFrame()
