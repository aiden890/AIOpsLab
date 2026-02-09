# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Trace Replayer

Replays trace data in realtime (excluding history data).
Sends traces to Jaeger with configurable speed factor.
"""

import time
import requests


class TraceReplayer:
    """Realtime trace replayer"""

    def __init__(self, adapter, jaeger_endpoint: str, namespace: str,
                 speed_factor: float, time_remapper):
        """
        Initialize trace replayer

        Args:
            adapter: Dataset adapter with load_traces() method
            jaeger_endpoint: Jaeger collector endpoint
            namespace: Namespace for service tagging
            speed_factor: Replay speed (1.0 = realtime, 10.0 = 10x faster)
            time_remapper: TimeRemapper instance
        """
        self.adapter = adapter
        self.jaeger_endpoint = jaeger_endpoint
        self.namespace = namespace
        self.speed_factor = speed_factor
        self.time_remapper = time_remapper

    def replay(self):
        """Start realtime replay"""
        print(f"[Trace Replayer] Starting realtime replay...")
        print(f"  Speed factor: {self.speed_factor}x")
        print(f"  Namespace: {self.namespace}")

        start_time_real = time.time()
        anchor_original = self.time_remapper.mapping['anchor_original']
        spans_replayed = 0
        batch = []
        batch_size = 100

        for chunk in self.adapter.load_traces():
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

                # Create span with remapped timestamp
                simulation_ts = self.time_remapper.remap_timestamp(row['timestamp'])

                span = {
                    "traceID": row['trace_id'],
                    "spanID": row['span_id'],
                    "operationName": row['operation_name'],
                    "startTime": int(simulation_ts * 1e6),  # microseconds
                    "duration": int(row['duration'] * 1000),  # ms to microseconds
                    "tags": [
                        {"key": "service.name", "type": "string", "value": row['service']},
                        {"key": "namespace", "type": "string", "value": self.namespace},
                        {"key": "is_history", "type": "bool", "value": False},
                    ],
                    "process": {
                        "serviceName": row['service'],
                        "tags": []
                    }
                }

                # Add custom tags
                for key, value in row['tags'].items():
                    span["tags"].append({
                        "key": key,
                        "type": "string",
                        "value": str(value)
                    })

                # Add parent span reference
                if row.get('parent_span_id'):
                    span["references"] = [{
                        "refType": "CHILD_OF",
                        "traceID": row['trace_id'],
                        "spanID": row['parent_span_id']
                    }]

                batch.append(span)
                spans_replayed += 1

                # Submit batch when full
                if len(batch) >= batch_size:
                    self._submit_batch(batch)
                    batch = []

        # Submit remaining batch
        if batch:
            self._submit_batch(batch)

        print(f"[Trace Replayer] ✓ Replay completed! {spans_replayed} spans replayed")

    def _submit_batch(self, spans: list):
        """Submit span batch to Jaeger"""
        try:
            payload = {
                "data": [{
                    "traceID": spans[0]["traceID"],
                    "spans": spans
                }]
            }

            response = requests.post(
                self.jaeger_endpoint,
                json=payload,
                timeout=10
            )

            if response.status_code not in [200, 202]:
                print(f"[Trace Replayer] Submit error: {response.status_code}")

        except Exception as e:
            print(f"[Trace Replayer] Submit error: {e}")
