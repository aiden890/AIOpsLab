# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Time Remapper

Maps original dataset timestamps to simulation timestamps.
Supports different modes (realtime, manual, query_based) and anchor strategies.
"""

from datetime import datetime
from typing import Dict
from .base_query_parser import QueryResult


class TimeRemapper:
    """Maps original timestamps to simulation timestamps"""

    def __init__(self, config: Dict, query_info: QueryResult):
        """
        Initialize time remapper

        Args:
            config: Full configuration dictionary
            query_info: Parsed query result with time range and faults
        """
        self.config = config
        self.query_info = query_info
        self.time_mapping_config = config.get('time_mapping', {})

        # Calculate time mapping
        self.mapping = self._calculate_mapping()

    def _calculate_mapping(self) -> Dict:
        """Calculate replay window around the fault anchor.

        No timestamp conversion — data stays in original time.
        The replay config controls the initial data window:
          - pre_buffer_minutes: data before the fault anchor loaded at deploy
          - post_buffer_minutes: data after the fault anchor loaded at deploy

        Streaming adds data beyond init_end as real time passes.
        """
        anchor_strategy = self.time_mapping_config.get('anchor_strategy', 'fault_start')
        replay = self.config.get('replay', {})
        pre_buffer = replay.get('pre_buffer_minutes', 30) * 60
        post_buffer = replay.get('post_buffer_minutes', 30) * 60

        # Determine anchor point in original timeline (fault start)
        anchor_original = self._get_anchor_original(anchor_strategy)

        # Initial data window in original timeline
        init_start_original = anchor_original - pre_buffer
        init_end_original = anchor_original + post_buffer

        # Fault window in original timeline (no conversion needed)
        fault_start = anchor_original
        fault_end = fault_start + self.query_info.time_range['duration']

        return {
            'anchor_original': anchor_original,
            'time_offset': 0,
            'pre_buffer': pre_buffer,
            'post_buffer': post_buffer,
            'init_start_original': init_start_original,
            'init_end_original': init_end_original,
            'fault_start': fault_start,
            'fault_end': fault_end,
            'anchor_strategy': anchor_strategy,
        }

    def _get_anchor_original(self, strategy: str) -> int:
        """Get anchor point in original timeline"""
        if strategy == 'fault_start':
            # Use query start time
            return self.query_info.time_range['start']

        elif strategy == 'fault_detection':
            # Use first fault timestamp from record
            if self.query_info.faults:
                return self.query_info.faults[0]['timestamp']
            else:
                return self.query_info.time_range['start']

        elif strategy == 'data_start':
            # Use time before query start (for more history)
            return self.query_info.time_range['start'] - 1800

        elif strategy == 'custom':
            # Use query start as default for custom
            return self.query_info.time_range['start']

        else:
            raise ValueError(f"Unknown anchor strategy: {strategy}")

    def is_history(self, original_ts: float) -> bool:
        """Check if timestamp is before the fault anchor."""
        return original_ts < self.mapping['anchor_original']

    def is_in_fault_window(self, ts: float) -> bool:
        """Check if timestamp is within the fault window."""
        return self.mapping['fault_start'] <= ts <= self.mapping['fault_end']

    def get_summary(self) -> str:
        """Get human-readable replay summary"""
        m = self.mapping
        fmt = '%Y-%m-%d %H:%M:%S'
        return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Replay Summary (no timestamp conversion)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Anchor Strategy: {m['anchor_strategy']}

Init Window : {datetime.fromtimestamp(m['init_start_original']).strftime(fmt)} ~ {datetime.fromtimestamp(m['init_end_original']).strftime(fmt)}
Fault Anchor: {datetime.fromtimestamp(m['anchor_original']).strftime(fmt)}
Fault Window: {datetime.fromtimestamp(m['fault_start']).strftime(fmt)} ~ {datetime.fromtimestamp(m['fault_end']).strftime(fmt)}

Pre-buffer: {m['pre_buffer']//60} min | Post-buffer: {m['post_buffer']//60} min
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

    def get_mapping_dict(self) -> Dict:
        """Get mapping as dictionary for serialization"""
        return self.mapping.copy()
