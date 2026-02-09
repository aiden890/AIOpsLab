# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Jaeger Bulk Loader

Loads history trace data into Jaeger using batch submission.
"""

import requests
import pandas as pd
from typing import Iterator


class JaegerBulkLoader:
    """Bulk load traces into Jaeger"""

    def __init__(self, jaeger_endpoint: str, namespace: str):
        """
        Initialize Jaeger bulk loader

        Args:
            jaeger_endpoint: Jaeger collector endpoint (e.g., "http://localhost:14268/api/traces")
            namespace: Namespace for service tagging
        """
        self.jaeger_endpoint = jaeger_endpoint
        self.namespace = namespace

    def bulk_load(self, trace_iterator: Iterator[pd.DataFrame], time_remapper) -> int:
        """
        Bulk load history traces

        Args:
            trace_iterator: Iterator of standardized trace DataFrames
            time_remapper: TimeRemapper instance

        Returns:
            Total number of spans loaded
        """
        total_spans = 0
        batch_size = 1000
        spans = []

        print(f"[Bulk Trace] Starting bulk load to Jaeger...")

        for chunk in trace_iterator:
            if chunk.empty:
                continue

            # Filter only history data
            history_mask = chunk['timestamp'].apply(time_remapper.is_history)
            history_chunk = chunk[history_mask]

            if history_chunk.empty:
                continue

            # Create Jaeger span format
            for _, row in history_chunk.iterrows():
                simulation_ts = time_remapper.remap_timestamp(row['timestamp'])

                span = {
                    "traceID": row['trace_id'],
                    "spanID": row['span_id'],
                    "operationName": row['operation_name'],
                    "startTime": int(simulation_ts * 1e6),  # microseconds
                    "duration": int(row['duration'] * 1000),  # milliseconds to microseconds
                    "tags": [
                        {"key": "service.name", "type": "string", "value": row['service']},
                        {"key": "namespace", "type": "string", "value": self.namespace},
                        {"key": "is_history", "type": "bool", "value": True},
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

                # Add parent span if exists
                if row.get('parent_span_id'):
                    span["references"] = [{
                        "refType": "CHILD_OF",
                        "traceID": row['trace_id'],
                        "spanID": row['parent_span_id']
                    }]

                spans.append(span)
                total_spans += 1

                # Submit batch when full
                if len(spans) >= batch_size:
                    self._submit_batch(spans)
                    spans = []

        # Submit remaining spans
        if spans:
            self._submit_batch(spans)

        print(f"[Bulk Trace] ✓ Loaded {total_spans} trace spans")
        return total_spans

    def _submit_batch(self, spans: list):
        """Submit a batch of spans to Jaeger"""
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
                print(f"[Bulk Trace] Warning: Batch submission failed: {response.status_code}")

        except Exception as e:
            print(f"[Bulk Trace] Error submitting batch: {e}")
