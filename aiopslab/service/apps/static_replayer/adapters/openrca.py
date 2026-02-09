# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
OpenRCA Dataset Adapter

Adapter for OpenRCA datasets (Bank, Telecom, Market/cloudbed-1, Market/cloudbed-2).
Loads trace, log, and metric data in standardized format.
"""

from .base import BaseDatasetAdapter
from ..time_mapping.openrca_query_parser import OpenRCAQueryParser
import pandas as pd
from pathlib import Path
from typing import Iterator, Dict, List


class OpenRCAAdapter(BaseDatasetAdapter):
    """OpenRCA dataset adapter"""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.telemetry_path = self.dataset_path / "telemetry"

        if not self.telemetry_path.exists():
            raise FileNotFoundError(f"Telemetry path not found: {self.telemetry_path}")

    def get_query_parser(self):
        """Return OpenRCA query parser"""
        query_config = self.config.get('query', {})
        return OpenRCAQueryParser(self.dataset_path, query_config)

    def load_traces(self) -> Iterator[pd.DataFrame]:
        """Load OpenRCA trace_span.csv files"""
        date_folders = self._get_date_range()
        trace_files = self.data_mapping.get('trace_files', ['trace_span.csv'])

        for date_folder in date_folders:
            for trace_filename in trace_files:
                trace_file = self.telemetry_path / date_folder / trace_filename

                if not trace_file.exists():
                    continue

                # Read in chunks for memory efficiency
                for chunk in pd.read_csv(trace_file, chunksize=10000):
                    # Standardize format
                    standardized = pd.DataFrame({
                        'timestamp': chunk['timestamp'],
                        'trace_id': chunk['trace_id'],
                        'span_id': chunk['span_id'],
                        'parent_span_id': chunk.get('parent_span_id', ''),
                        'service': chunk.get('service', chunk.get('cmdb_id', 'unknown')),
                        'operation_name': chunk.get('operation_name', 'unknown'),
                        'duration': chunk.get('duration', 0),
                        'tags': chunk.apply(lambda row: {
                            'cmdb_id': row.get('cmdb_id', ''),
                            'status': row.get('status', 'ok'),
                            'has_error': row.get('has_error', False)
                        }, axis=1)
                    })

                    standardized = standardized.sort_values('timestamp')
                    yield standardized

    def load_logs(self) -> Iterator[pd.DataFrame]:
        """Load OpenRCA log_service.csv, log_proxy.csv files"""
        date_folders = self._get_date_range()
        log_files = self.data_mapping.get('log_files', ['log_service.csv'])

        for date_folder in date_folders:
            for log_filename in log_files:
                log_file = self.telemetry_path / date_folder / log_filename

                if not log_file.exists():
                    continue

                # Read in chunks
                for chunk in pd.read_csv(log_file, chunksize=10000):
                    # Standardize format
                    standardized = pd.DataFrame({
                        'timestamp': chunk['timestamp'],
                        'log_id': chunk['log_id'],
                        'cmdb_id': chunk.get('cmdb_id', 'unknown'),
                        'log_level': chunk.get('log_level', 'INFO'),
                        'message': chunk.get('value', chunk.get('message', '')),
                        'tags': chunk.apply(lambda row: {
                            'log_name': row.get('log_name', ''),
                            'log_type': log_filename.replace('.csv', ''),
                            'source_file': log_filename
                        }, axis=1)
                    })

                    standardized = standardized.sort_values('timestamp')
                    yield standardized

    def load_metrics(self) -> Iterator[pd.DataFrame]:
        """Load OpenRCA metric_*.csv files"""
        date_folders = self._get_date_range()
        metric_files = self.data_mapping.get('metric_files', [])

        for date_folder in date_folders:
            for metric_filename in metric_files:
                metric_file = self.telemetry_path / date_folder / metric_filename

                if not metric_file.exists():
                    continue

                # Read in chunks
                for chunk in pd.read_csv(metric_file, chunksize=10000):
                    # Normalize based on metric file format
                    standardized = self._normalize_metric_chunk(chunk, metric_filename)

                    if standardized is not None and not standardized.empty:
                        standardized = standardized.sort_values('timestamp')
                        yield standardized

    def _normalize_metric_chunk(self, chunk: pd.DataFrame, filename: str) -> pd.DataFrame:
        """
        Normalize metric chunk to standard format

        OpenRCA has different metric formats:
        1. Wide format (metric_service, metric_app): multiple columns as separate metrics
        2. Long format (metric_container, metric_node): kpi_name column
        """
        records = []

        # Wide format: metric_service.csv, metric_app.csv
        if 'rr' in chunk.columns or 'sr' in chunk.columns or 'mrt' in chunk.columns:
            for _, row in chunk.iterrows():
                timestamp = row['timestamp']
                service = row.get('service', row.get('cmdb_id', 'unknown'))

                # Extract all metric columns
                for col in ['rr', 'sr', 'mrt', 'count', 'tc']:
                    if col in row and pd.notna(row[col]):
                        records.append({
                            'timestamp': timestamp,
                            'metric_name': f"{filename.replace('.csv', '')}_{col}",
                            'value': float(row[col]),
                            'labels': {'service': service, 'source': filename}
                        })

        # Long format: metric_container.csv, metric_node.csv, metric_runtime.csv, metric_mesh.csv
        elif 'kpi_name' in chunk.columns:
            for _, row in chunk.iterrows():
                records.append({
                    'timestamp': row['timestamp'],
                    'metric_name': row['kpi_name'],
                    'value': float(row['value']),
                    'labels': {
                        'cmdb_id': row.get('cmdb_id', ''),
                        'service': row.get('service', ''),
                        'source': filename
                    }
                })

        # Middleware format: itemid, name, bomc_id, timestamp, value, cmdb_id
        elif 'itemid' in chunk.columns and 'name' in chunk.columns:
            for _, row in chunk.iterrows():
                records.append({
                    'timestamp': row['timestamp'],
                    'metric_name': row['name'],
                    'value': float(row['value']),
                    'labels': {
                        'itemid': str(row.get('itemid', '')),
                        'bomc_id': row.get('bomc_id', ''),
                        'cmdb_id': row.get('cmdb_id', ''),
                        'source': filename
                    }
                })

        # Default format
        elif 'value' in chunk.columns:
            for _, row in chunk.iterrows():
                records.append({
                    'timestamp': row.get('timestamp', 0),
                    'metric_name': filename.replace('.csv', ''),
                    'value': float(row['value']),
                    'labels': {k: str(v) for k, v in row.items() if k not in ['timestamp', 'value']}
                })

        if records:
            return pd.DataFrame(records)
        return None

    def _get_date_range(self) -> List[str]:
        """Get list of date folders to process based on config"""
        replay_config = self.config.get('replay_config', {})
        start_date = replay_config.get('start_date')
        end_date = replay_config.get('end_date')

        # Get all date folders
        all_folders = sorted([f.name for f in self.telemetry_path.iterdir() if f.is_dir()])

        # Filter by date range if specified
        if start_date and end_date:
            filtered = [f for f in all_folders if start_date <= f <= end_date]
            return filtered if filtered else all_folders

        return all_folders
