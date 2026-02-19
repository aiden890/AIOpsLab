# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
OpenRCA Query Parser

Parses OpenRCA dataset query.csv and record.csv files.
Extracts time ranges from instruction text using regex patterns.
"""

from .base_query_parser import BaseQueryParser, QueryResult
import pandas as pd
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List
from pathlib import Path

# OpenRCA dataset timestamps are in UTC+8 (Asia/Shanghai)
_UTC_PLUS_8 = timezone(timedelta(hours=8))
_UTC = timezone.utc


def convert_scoring_points_to_utc(scoring_points: str) -> str:
    """Convert all UTC+8 datetime strings (YYYY-MM-DD HH:MM:SS) in scoring_points to UTC."""
    def _to_utc(m):
        try:
            dt = datetime.strptime(m.group(0), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC_PLUS_8)
            return dt.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return m.group(0)
    return re.sub(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', _to_utc, scoring_points)


class OpenRCAQueryParser(BaseQueryParser):
    """OpenRCA dataset-specific query parser"""

    def __init__(self, dataset_path: Path, config: Dict):
        super().__init__(dataset_path, config)

        # Load query.csv and record.csv
        self.query_file = self.dataset_path / config.get('query_file', 'query.csv')
        self.record_file = self.dataset_path / config.get('record_file', 'record.csv')

        if not self.query_file.exists():
            raise FileNotFoundError(f"Query file not found: {self.query_file}")
        if not self.record_file.exists():
            raise FileNotFoundError(f"Record file not found: {self.record_file}")

        self.query_df = pd.read_csv(self.query_file)
        self.record_df = pd.read_csv(self.record_file)

    def list_tasks(self) -> List[str]:
        """List all available tasks"""
        return self.query_df['task_index'].tolist()

    def parse_task(self, task_identifier: str) -> QueryResult:
        """
        Parse OpenRCA task

        Args:
            task_identifier: "task_1", "task_2", etc.

        Returns:
            QueryResult with parsed time range and faults
        """
        # Get task row
        task_row = self.query_df[self.query_df['task_index'] == task_identifier]

        if task_row.empty:
            raise ValueError(f"Task not found: {task_identifier}")

        task_row = task_row.iloc[0]
        instruction = task_row['instruction']

        # Extract time range from instruction
        time_range = self._extract_time_range_from_instruction(instruction)

        # Extract faults from record.csv
        faults = self._extract_faults(time_range)

        # Metadata
        metadata = {
            'instruction': instruction,
            'scoring_points': task_row.get('scoring_points', ''),
            'dataset_type': 'openrca',
            'dataset_name': self.dataset_path.name
        }

        return QueryResult(
            task_id=task_identifier,
            time_range=time_range,
            faults=faults,
            metadata=metadata
        )

    def _extract_time_range_from_instruction(self, instruction: str) -> Dict:
        """
        Extract time range from instruction text

        Supported formats:
        - "March 4, 2021, within the time range of 14:30 to 15:00"
        - "April 11, 2020, from 00:00 to 00:30"
        - "between 2021-03-04 14:30:00 and 2021-03-04 15:00:00"
        - "on 2022-03-20 from 09:00 to 10:00"
        """
        patterns = [
            # Pattern 1: "March 4, 2021" + "14:30 to 15:00"
            (r'(\w+ \d+, \d{4}).*?(\d{1,2}:\d{2})\s+to\s+(\d{1,2}:\d{2})', 'month_day_year_time'),
            # Pattern 2: "April 11, 2020" + "from 00:00 to 00:30"
            (r'(\w+ \d+, \d{4}).*?from\s+(\d{1,2}:\d{2})\s+to\s+(\d{1,2}:\d{2})', 'month_day_year_time'),
            # Pattern 3: Full datetime
            (r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(?:and|to)\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', 'full_datetime'),
            # Pattern 4: "on 2022-03-20 from 09:00 to 10:00"
            (r'on\s+(\d{4}-\d{2}-\d{2}).*?from\s+(\d{1,2}:\d{2})\s+to\s+(\d{1,2}:\d{2})', 'date_time_range'),
            # Pattern 5: "between 14:30 and 15:00 on March 4, 2021"
            (r'between\s+(\d{1,2}:\d{2})\s+and\s+(\d{1,2}:\d{2}).*?on\s+(\w+ \d+, \d{4})', 'time_first'),
        ]

        for pattern, pattern_type in patterns:
            match = re.search(pattern, instruction, re.IGNORECASE)
            if match:
                return self._parse_time_match(match, pattern_type)

        # Parsing failed - use record.csv as fallback
        print(f"Warning: Could not parse time from instruction, using record.csv")
        return self._extract_from_records()

    def _parse_time_match(self, match, pattern_type: str) -> Dict:
        """Parse regex match to timestamp"""
        groups = match.groups()

        try:
            if pattern_type == 'month_day_year_time':
                # "March 4, 2021" + "14:30" + "15:00"
                date_str = groups[0]
                start_time_str = groups[1]
                end_time_str = groups[2]

                # Parse month name to number
                date_obj = datetime.strptime(date_str, "%B %d, %Y")

                start_dt = datetime.strptime(
                    f"{date_obj.date()} {start_time_str}",
                    "%Y-%m-%d %H:%M"
                ).replace(tzinfo=_UTC_PLUS_8)
                end_dt = datetime.strptime(
                    f"{date_obj.date()} {end_time_str}",
                    "%Y-%m-%d %H:%M"
                ).replace(tzinfo=_UTC_PLUS_8)

            elif pattern_type == 'full_datetime':
                # "2021-03-04 14:30:00" + "2021-03-04 15:00:00"
                start_dt = datetime.strptime(groups[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC_PLUS_8)
                end_dt = datetime.strptime(groups[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC_PLUS_8)

            elif pattern_type == 'date_time_range':
                # "2022-03-20" + "09:00" + "10:00"
                date_str = groups[0]
                start_time_str = groups[1]
                end_time_str = groups[2]

                start_dt = datetime.strptime(f"{date_str} {start_time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=_UTC_PLUS_8)
                end_dt = datetime.strptime(f"{date_str} {end_time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=_UTC_PLUS_8)

            elif pattern_type == 'time_first':
                # "14:30" + "15:00" + "March 4, 2021"
                start_time_str = groups[0]
                end_time_str = groups[1]
                date_str = groups[2]

                date_obj = datetime.strptime(date_str, "%B %d, %Y")

                start_dt = datetime.strptime(
                    f"{date_obj.date()} {start_time_str}",
                    "%Y-%m-%d %H:%M"
                ).replace(tzinfo=_UTC_PLUS_8)
                end_dt = datetime.strptime(
                    f"{date_obj.date()} {end_time_str}",
                    "%Y-%m-%d %H:%M"
                ).replace(tzinfo=_UTC_PLUS_8)

            return {
                'start': int(start_dt.timestamp()),
                'end': int(end_dt.timestamp()),
                'start_str': start_dt.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S"),
                'end_str': end_dt.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S"),
                'duration': int((end_dt - start_dt).total_seconds()),
                '_start_dt': start_dt,
                '_end_dt': end_dt,
            }

        except Exception as e:
            print(f"Warning: Failed to parse time match: {e}")
            return self._extract_from_records()

    def _extract_from_records(self) -> Dict:
        """Extract time range from record.csv (fallback)"""
        if self.record_df.empty:
            raise ValueError("Cannot extract time range: both instruction and record.csv are empty")

        # Use timestamp column or parse datetime column
        if 'timestamp' in self.record_df.columns:
            min_ts = self.record_df['timestamp'].min()
            max_ts = self.record_df['timestamp'].max()
        else:
            # Parse datetime strings as UTC+8
            datetimes = pd.to_datetime(self.record_df['datetime'])
            min_ts = datetimes.min().tz_localize(_UTC_PLUS_8).timestamp()
            max_ts = datetimes.max().tz_localize(_UTC_PLUS_8).timestamp()

        # Expand time range to 30 minutes before/after faults
        min_ts -= 1800  # 30 minutes before
        max_ts += 1800  # 30 minutes after

        start_dt = datetime.fromtimestamp(min_ts, tz=_UTC_PLUS_8)
        end_dt = datetime.fromtimestamp(max_ts, tz=_UTC_PLUS_8)
        return {
            'start': int(min_ts),
            'end': int(max_ts),
            'start_str': start_dt.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S"),
            'end_str': end_dt.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S"),
            'duration': int(max_ts - min_ts),
            '_start_dt': start_dt,
            '_end_dt': end_dt,
        }

    def _rewrite_instruction_to_utc(self, instruction: str, start_dt: datetime, end_dt: datetime) -> str:
        """Replace UTC+8 time mentions in instruction with UTC equivalents."""
        start_utc = start_dt.astimezone(_UTC)
        end_utc = end_dt.astimezone(_UTC)
        result = instruction

        # Replace HH:MM time strings
        result = result.replace(start_dt.strftime("%H:%M"), start_utc.strftime("%H:%M"))
        result = result.replace(end_dt.strftime("%H:%M"), end_utc.strftime("%H:%M"))

        # Replace HH:MM:SS if present
        result = result.replace(start_dt.strftime("%H:%M:%S"), start_utc.strftime("%H:%M:%S"))
        result = result.replace(end_dt.strftime("%H:%M:%S"), end_utc.strftime("%H:%M:%S"))

        # Replace date strings if UTC date differs from UTC+8 date
        if start_dt.date() != start_utc.date():
            orig = f"{start_dt.strftime('%B')} {start_dt.day}, {start_dt.year}"
            new = f"{start_utc.strftime('%B')} {start_utc.day}, {start_utc.year}"
            result = result.replace(orig, new)
            result = result.replace(start_dt.strftime("%Y-%m-%d"), start_utc.strftime("%Y-%m-%d"))

        return result

    def convert_query_row_to_utc(self, query_row: dict) -> dict:
        """Return a copy of query_row with instruction and scoring_points converted to UTC."""
        row = dict(query_row)
        instruction = row.get('instruction', '')
        scoring_points = row.get('scoring_points', '')

        # Parse time range from instruction to get UTC+8 datetimes for rewriting
        try:
            time_range = self._extract_time_range_from_instruction(instruction)
            start_dt = time_range.get('_start_dt')
            end_dt = time_range.get('_end_dt')
            if start_dt and end_dt:
                row['instruction'] = self._rewrite_instruction_to_utc(instruction, start_dt, end_dt)
        except Exception as e:
            print(f"Warning: Could not rewrite instruction to UTC: {e}")

        row['scoring_points'] = convert_scoring_points_to_utc(scoring_points)
        return row

    def _extract_faults(self, time_range: Dict) -> List[Dict]:
        """Extract faults from record.csv within time range"""
        faults = []

        for _, row in self.record_df.iterrows():
            # Extract timestamp
            if 'timestamp' in row:
                ts = row['timestamp']
            else:
                ts = datetime.strptime(row['datetime'], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC_PLUS_8).timestamp()

            # Only include faults within time range
            if time_range['start'] <= ts <= time_range['end']:
                # Convert datetime string to UTC (record.csv stores UTC+8 strings)
                raw_dt_str = row.get('datetime', '')
                if raw_dt_str:
                    try:
                        dt_utc8 = datetime.strptime(raw_dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_UTC_PLUS_8)
                        dt_str = dt_utc8.astimezone(_UTC).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        dt_str = raw_dt_str
                else:
                    dt_str = datetime.fromtimestamp(ts, tz=_UTC).strftime("%Y-%m-%d %H:%M:%S")
                faults.append({
                    'timestamp': int(ts),
                    'datetime': dt_str,
                    'level': row.get('level', 'unknown'),
                    'component': row['component'],
                    'reason': row.get('reason', 'unknown')
                })

        return sorted(faults, key=lambda x: x['timestamp'])
