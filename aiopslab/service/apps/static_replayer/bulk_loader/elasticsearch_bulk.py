# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Elasticsearch Bulk Loader

Loads history log data into Elasticsearch using Bulk API.
"""

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
import pandas as pd
from typing import Iterator
from datetime import datetime


class ElasticsearchBulkLoader:
    """Bulk load logs into Elasticsearch"""

    def __init__(self, es_url: str, namespace: str):
        """
        Initialize Elasticsearch bulk loader

        Args:
            es_url: Elasticsearch URL (e.g., "http://localhost:9200")
            namespace: Namespace for index prefix
        """
        self.es = Elasticsearch([es_url], request_timeout=30)
        self.namespace = namespace
        self.index_prefix = f"logstash-{namespace}"

    def bulk_load(self, log_iterator: Iterator[pd.DataFrame], time_remapper) -> int:
        """
        Bulk load history logs

        Args:
            log_iterator: Iterator of standardized log DataFrames
            time_remapper: TimeRemapper instance for timestamp conversion

        Returns:
            Total number of documents loaded
        """
        total_docs = 0
        batch_size = 1000
        actions = []

        print(f"[Bulk Log] Starting bulk load to Elasticsearch...")

        for chunk in log_iterator:
            if chunk.empty:
                continue

            # Filter only history data
            history_mask = chunk['timestamp'].apply(time_remapper.is_history)
            history_chunk = chunk[history_mask]

            if history_chunk.empty:
                continue

            # Create bulk actions
            for _, row in history_chunk.iterrows():
                # Remap timestamp
                simulation_ts = time_remapper.remap_timestamp(row['timestamp'])

                # Date-based index
                index_name = f"{self.index_prefix}-{datetime.fromtimestamp(simulation_ts).strftime('%Y.%m.%d')}"

                action = {
                    '_index': index_name,
                    '_source': {
                        '@timestamp': datetime.fromtimestamp(simulation_ts).isoformat(),
                        'log_id': row['log_id'],
                        'cmdb_id': row['cmdb_id'],
                        'log_level': row['log_level'],
                        'message': row['message'],
                        'namespace': self.namespace,
                        'is_history': True,
                        **row['tags']
                    }
                }
                actions.append(action)

                # Bulk insert when batch is full
                if len(actions) >= batch_size:
                    success, failed = self._bulk_insert(actions)
                    total_docs += success
                    actions = []

            # Insert remaining actions
            if actions:
                success, failed = self._bulk_insert(actions)
                total_docs += success
                actions = []

        print(f"[Bulk Log] ✓ Loaded {total_docs} log documents")
        return total_docs

    def _bulk_insert(self, actions: list) -> tuple:
        """Execute bulk insert"""
        try:
            success, errors = bulk(self.es, actions, raise_on_error=False)
            failed = len(errors) if errors else 0
            if failed > 0:
                print(f"[Bulk Log] Warning: {failed} documents failed")
            return success, failed
        except Exception as e:
            print(f"[Bulk Log] Error during bulk insert: {e}")
            return 0, len(actions)
