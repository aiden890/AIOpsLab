# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""OpenRCA dataset loader for query and record files."""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
import pandas as pd

logger = logging.getLogger(__name__)


class OpenRCALoader:
    """
    Loader for OpenRCA dataset files.

    Loads query.csv (task definitions) and record.csv (ground truth).
    Handles timestamp remapping from historical to current timeline.
    """

    def __init__(self, dataset_path: Path):
        """
        Initialize loader with dataset directory.

        Args:
            dataset_path: Path to dataset directory (e.g., openrca_dataset/Bank/)
        """
        self.dataset_path = Path(dataset_path)
        self.query_df: Optional[pd.DataFrame] = None
        self.record_df: Optional[pd.DataFrame] = None
        self.time_mapper: Optional[Any] = None  # Set after replayer starts

        logger.info(f"OpenRCALoader initialized for: {self.dataset_path}")

    def set_time_mapper(self, time_mapper: Any):
        """
        Set TimeMapper for timestamp remapping.

        Args:
            time_mapper: TimeMapper instance from replayer
        """
        self.time_mapper = time_mapper
        logger.debug("TimeMapper set for loader")

    def load_query_csv(self) -> pd.DataFrame:
        """
        Load query.csv with task definitions.

        Returns:
            DataFrame with columns: task_index, instruction, scoring_points

        Raises:
            FileNotFoundError: If query.csv doesn't exist
        """
        query_path = self.dataset_path / 'query.csv'

        if not query_path.exists():
            raise FileNotFoundError(f"query.csv not found: {query_path}")

        self.query_df = pd.read_csv(query_path)
        logger.info(f"Loaded {len(self.query_df)} tasks from query.csv")

        return self.query_df

    def load_record_csv(self) -> pd.DataFrame:
        """
        Load record.csv with ground truth.

        Returns:
            DataFrame with columns: level, component, timestamp, datetime, reason

        Raises:
            FileNotFoundError: If record.csv doesn't exist
        """
        record_path = self.dataset_path / 'record.csv'

        if not record_path.exists():
            raise FileNotFoundError(f"record.csv not found: {record_path}")

        self.record_df = pd.read_csv(record_path)
        logger.info(f"Loaded {len(self.record_df)} ground truth records from record.csv")

        return self.record_df

    def get_task_info(self, task_index: str) -> Dict[str, str]:
        """
        Get task definition by task index.

        Args:
            task_index: Task identifier (e.g., 'task_1', 'task_6')

        Returns:
            Dictionary with 'instruction' and 'scoring_points'

        Raises:
            ValueError: If task not found
        """
        if self.query_df is None:
            self.load_query_csv()

        # Filter for specific task
        task_rows = self.query_df[self.query_df['task_index'] == task_index]

        if len(task_rows) == 0:
            raise ValueError(f"Task not found: {task_index}")

        # Return first match
        task = task_rows.iloc[0]

        return {
            'instruction': task['instruction'],
            'scoring_points': task['scoring_points']
        }

    def parse_time_window_from_instruction(self, instruction: str) -> tuple[datetime, datetime]:
        """
        Parse time window from task instruction and remap to current timeline.

        Args:
            instruction: Task instruction text (e.g., "March 4, 2021, between 14:30 and 15:00...")

        Returns:
            Tuple of (start_time, end_time) in CURRENT timeline

        Raises:
            ValueError: If time window cannot be parsed
            RuntimeError: If TimeMapper not set
        """
        if not self.time_mapper:
            raise RuntimeError("TimeMapper not set. Call set_time_mapper() after replayer starts.")

        # Pattern: "March 4, 2021, between 14:30 and 15:00"
        # or "March 4, 2021, within the time range of 14:30 to 15:00"
        pattern = r'(\w+ \d+, \d+),.*?(\d+:\d+).*?(\d+:\d+)'
        match = re.search(pattern, instruction)

        if not match:
            raise ValueError(f"Cannot parse time window from instruction: {instruction}")

        date_str = match.group(1)
        start_time_str = match.group(2)
        end_time_str = match.group(3)

        # Parse original (historical) times
        original_start = datetime.strptime(
            f"{date_str} {start_time_str}",
            "%B %d, %Y %H:%M"
        )
        original_end = datetime.strptime(
            f"{date_str} {end_time_str}",
            "%B %d, %Y %H:%M"
        )

        logger.debug(f"Parsed original time window: {original_start} - {original_end}")

        # Remap to current timeline
        current_start_ts = self.time_mapper.remap_to_timestamp(original_start)
        current_end_ts = self.time_mapper.remap_to_timestamp(original_end)

        current_start = datetime.fromtimestamp(current_start_ts)
        current_end = datetime.fromtimestamp(current_end_ts)

        logger.info(f"Remapped to current timeline: {current_start} - {current_end}")

        return current_start, current_end

    def get_ground_truth(
        self,
        start_time: datetime,
        end_time: datetime
    ) -> Optional[Dict[str, Any]]:
        """
        Get ground truth record for given time window.

        Args:
            start_time: Start of incident window (CURRENT timeline)
            end_time: End of incident window (CURRENT timeline)

        Returns:
            Ground truth dictionary with timestamps in CURRENT timeline
        """
        if self.record_df is None:
            self.load_record_csv()

        if not self.time_mapper:
            raise RuntimeError("TimeMapper not set. Call set_time_mapper() after replayer starts.")

        # Convert current times back to original timeline for matching
        original_start_ts = self.time_mapper.remap_to_original(start_time.timestamp())
        original_end_ts = self.time_mapper.remap_to_original(end_time.timestamp())

        original_start_dt = datetime.fromtimestamp(original_start_ts)
        original_end_dt = datetime.fromtimestamp(original_end_ts)

        logger.info(f"Searching ground truth:")
        logger.info(f"  Current window: {start_time} - {end_time}")
        logger.info(f"  Original window: {original_start_dt} - {original_end_dt}")
        logger.info(f"  Original timestamps: {original_start_ts} - {original_end_ts}")

        # Find records within original time window
        matches = self.record_df[
            (self.record_df['timestamp'] >= original_start_ts) &
            (self.record_df['timestamp'] <= original_end_ts)
        ]

        logger.info(f"  Found {len(matches)} matching records")

        if len(matches) == 0:
            logger.warning(f"No ground truth found for window: {start_time} - {end_time}")
            logger.warning(f"Record timestamp range: {self.record_df['timestamp'].min()} - {self.record_df['timestamp'].max()}")
            return None

        if len(matches) > 1:
            logger.warning(f"Multiple ground truth records found ({len(matches)}), using first")

        # Get first match
        record = matches.iloc[0]

        # Remap ground truth timestamp to CURRENT timeline
        current_gt_ts = self.time_mapper.remap_to_timestamp(record['timestamp'])
        current_gt_dt = datetime.fromtimestamp(current_gt_ts)

        logger.info(f"Ground truth found: {record['component']} at {current_gt_dt} ({record['reason']})")

        return {
            'component': record['component'],
            'timestamp': current_gt_ts,  # Current timeline
            'datetime': current_gt_dt.strftime('%Y-%m-%d %H:%M:%S'),  # Current timeline
            'reason': record['reason'],
            'level': record.get('level', 'pod')  # Some datasets may not have this field
        }
