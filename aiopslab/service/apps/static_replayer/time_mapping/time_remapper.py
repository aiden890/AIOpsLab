# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Time Remapper

Maps original dataset timestamps to simulation timestamps.
Supports different modes (realtime, manual, query_based) and anchor strategies.
"""

from datetime import datetime, timedelta
from typing import Dict, Optional
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
        """Calculate time mapping from original to simulation timestamps"""
        mode = self.time_mapping_config.get('mode', 'realtime')
        anchor_strategy = self.time_mapping_config.get('anchor_strategy', 'fault_start')

        # 1. Determine anchor point in original timeline
        anchor_original = self._get_anchor_original(anchor_strategy)

        # 2. Determine anchor point in simulation timeline
        anchor_simulation = self._get_anchor_simulation(mode)

        # 3. Calculate time offset
        time_offset = anchor_simulation - anchor_original
        time_offset += self.time_mapping_config.get('time_offset_seconds', 0)

        # 4. Calculate history range
        history_duration = self.time_mapping_config.get('history_duration_seconds', 1800)
        history_start_original = anchor_original - history_duration
        history_start_simulation = anchor_simulation - history_duration

        # 5. Calculate fault time range in simulation
        fault_start_simulation = anchor_simulation
        fault_end_simulation = fault_start_simulation + self.query_info.time_range['duration']

        return {
            'anchor_original': anchor_original,
            'anchor_simulation': anchor_simulation,
            'time_offset': time_offset,
            'history_start_original': history_start_original,
            'history_start_simulation': history_start_simulation,
            'history_duration': history_duration,
            'fault_start_simulation': fault_start_simulation,
            'fault_end_simulation': fault_end_simulation,
            'mode': mode,
            'anchor_strategy': anchor_strategy
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

    def _get_anchor_simulation(self, mode: str) -> int:
        """Get anchor point in simulation timeline"""
        if mode == 'realtime':
            # Use current time
            return int(datetime.now().timestamp())

        elif mode == 'manual':
            # Use user-specified time
            start_time_str = self.time_mapping_config.get('simulation_start_time')
            if not start_time_str:
                raise ValueError("Manual mode requires 'simulation_start_time' in config")

            # Parse ISO format datetime
            start_dt = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
            return int(start_dt.timestamp())

        elif mode == 'query_based':
            # Use current time (same as realtime)
            return int(datetime.now().timestamp())

        else:
            raise ValueError(f"Unknown time mapping mode: {mode}")

    def remap_timestamp(self, original_ts: float) -> float:
        """
        Convert original timestamp to simulation timestamp

        Args:
            original_ts: Original Unix timestamp

        Returns:
            Simulation Unix timestamp
        """
        return original_ts + self.mapping['time_offset']

    def is_history(self, original_ts: float) -> bool:
        """
        Check if timestamp is in history range (before anchor point)

        Args:
            original_ts: Original Unix timestamp

        Returns:
            True if in history range, False otherwise
        """
        return original_ts < self.mapping['anchor_original']

    def is_in_fault_window(self, simulation_ts: float) -> bool:
        """
        Check if simulation timestamp is within fault window

        Args:
            simulation_ts: Simulation Unix timestamp

        Returns:
            True if within fault window, False otherwise
        """
        return (self.mapping['fault_start_simulation'] <= simulation_ts <=
                self.mapping['fault_end_simulation'])

    def get_summary(self) -> str:
        """Get human-readable time mapping summary"""
        return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Time Mapping Summary
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Mode: {self.mapping['mode']}
Anchor Strategy: {self.mapping['anchor_strategy']}

Original Timeline:
  History Start : {datetime.fromtimestamp(self.mapping['history_start_original']).strftime('%Y-%m-%d %H:%M:%S')}
  Anchor Point  : {datetime.fromtimestamp(self.mapping['anchor_original']).strftime('%Y-%m-%d %H:%M:%S')}

Simulation Timeline:
  History Start : {datetime.fromtimestamp(self.mapping['history_start_simulation']).strftime('%Y-%m-%d %H:%M:%S')}
  Anchor Point  : {datetime.fromtimestamp(self.mapping['anchor_simulation']).strftime('%Y-%m-%d %H:%M:%S')}
  Fault End     : {datetime.fromtimestamp(self.mapping['fault_end_simulation']).strftime('%Y-%m-%d %H:%M:%S')}

Time Offset: {self.mapping['time_offset']} seconds ({self.mapping['time_offset']/3600:.2f} hours)
History Duration: {self.mapping['history_duration']} seconds ({self.mapping['history_duration']/60:.0f} minutes)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

    def get_mapping_dict(self) -> Dict:
        """Get mapping as dictionary for serialization"""
        return self.mapping.copy()
