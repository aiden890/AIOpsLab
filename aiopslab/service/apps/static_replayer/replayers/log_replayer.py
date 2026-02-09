# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Log Replayer

Replays log data in realtime (excluding history data).
Sends logs to Elasticsearch with configurable speed factor.
"""

import time
from elasticsearch import Elasticsearch
from datetime import datetime


class LogReplayer:
    """Realtime log replayer"""

    def __init__(self, adapter, es_url: str, namespace: str,
                 speed_factor: float, time_remapper):
        """
        Initialize log replayer

        Args:
            adapter: Dataset adapter with load_logs() method
            es_url: Elasticsearch URL
            namespace: Namespace for index naming
            speed_factor: Replay speed (1.0 = realtime, 10.0 = 10x faster)
            time_remapper: TimeRemapper instance
        """
        self.adapter = adapter
        self.es = Elasticsearch([es_url], request_timeout=30)
        self.namespace = namespace
        self.speed_factor = speed_factor
        self.time_remapper = time_remapper
        self.index_prefix = f"logstash-{namespace}"

    def replay(self):
        """Start realtime replay"""
        print(f"[Log Replayer] Starting realtime replay...")
        print(f"  Speed factor: {self.speed_factor}x")
        print(f"  Namespace: {self.namespace}")

        start_time_real = time.time()
        anchor_original = self.time_remapper.mapping['anchor_original']
        logs_replayed = 0

        for chunk in self.adapter.load_logs():
            if chunk.empty:
                continue

            # Filter out history data
            realtime_mask = ~chunk['timestamp'].apply(self.time_remapper.is_history)
            realtime_chunk = chunk[realtime_mask]

            if realtime_chunk.empty:
                continue

            for _, row in realtime_chunk.iterrows():
                # Calculate elapsed time
                elapsed_data_time = row['timestamp'] - anchor_original
                elapsed_real_time = time.time() - start_time_real

                # Wait according to speed factor
                target_real_time = elapsed_data_time / self.speed_factor
                sleep_time = target_real_time - elapsed_real_time

                if sleep_time > 0:
                    time.sleep(sleep_time)

                # Index log with remapped timestamp
                simulation_ts = self.time_remapper.remap_timestamp(row['timestamp'])

                index_name = f"{self.index_prefix}-{datetime.fromtimestamp(simulation_ts).strftime('%Y.%m.%d')}"

                doc = {
                    '@timestamp': datetime.fromtimestamp(simulation_ts).isoformat(),
                    'log_id': row['log_id'],
                    'cmdb_id': row['cmdb_id'],
                    'log_level': row['log_level'],
                    'message': row['message'],
                    'namespace': self.namespace,
                    'is_history': False,
                    **row['tags']
                }

                try:
                    self.es.index(index=index_name, document=doc)
                    logs_replayed += 1
                except Exception as e:
                    print(f"[Log Replayer] Index error: {e}")

        print(f"[Log Replayer] ✓ Replay completed! {logs_replayed} logs replayed")
