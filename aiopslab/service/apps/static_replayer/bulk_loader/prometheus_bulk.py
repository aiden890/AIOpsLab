# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Prometheus Bulk Loader

Loads history metric data into Prometheus using Pushgateway.
Note: Prometheus has limited support for historical data, so we use Pushgateway.
"""

import requests
import pandas as pd
from typing import Iterator
from datetime import datetime
import time


class PrometheusBulkLoader:
    """Bulk load metrics into Prometheus via Pushgateway"""

    def __init__(self, pushgateway_url: str, namespace: str):
        """
        Initialize Prometheus bulk loader

        Args:
            pushgateway_url: Pushgateway URL (e.g., "http://localhost:9091")
            namespace: Namespace/job name for metrics
        """
        self.pushgateway_url = pushgateway_url.rstrip('/')
        self.namespace = namespace

    def bulk_load(self, metric_iterator: Iterator[pd.DataFrame], time_remapper) -> int:
        """
        Bulk load history metrics

        Args:
            metric_iterator: Iterator of standardized metric DataFrames
            time_remapper: TimeRemapper instance

        Returns:
            Total number of metric samples loaded
        """
        total_samples = 0
        batch_size = 500
        metric_batches = {}

        print(f"[Bulk Metric] Starting bulk load to Prometheus...")

        for chunk in metric_iterator:
            if chunk.empty:
                continue

            # Filter only history data
            history_mask = chunk['timestamp'].apply(time_remapper.is_history)
            history_chunk = chunk[history_mask]

            if history_chunk.empty:
                continue

            # Group by metric name for efficient pushing
            for _, row in history_chunk.iterrows():
                simulation_ts = time_remapper.remap_timestamp(row['timestamp'])
                metric_name = row['metric_name']

                if metric_name not in metric_batches:
                    metric_batches[metric_name] = []

                # Create Prometheus metric line
                labels = row['labels'].copy()
                labels['is_history'] = 'true'
                labels['namespace'] = self.namespace

                label_str = ','.join([f'{k}="{v}"' for k, v in labels.items()])
                metric_line = f"{metric_name}{{{label_str}}} {row['value']} {int(simulation_ts * 1000)}"

                metric_batches[metric_name].append(metric_line)
                total_samples += 1

                # Push batch when it reaches size limit
                if len(metric_batches[metric_name]) >= batch_size:
                    self._push_batch(metric_name, metric_batches[metric_name])
                    metric_batches[metric_name] = []

        # Push remaining batches
        for metric_name, lines in metric_batches.items():
            if lines:
                self._push_batch(metric_name, lines)

        print(f"[Bulk Metric] ✓ Loaded {total_samples} metric samples")
        return total_samples

    def _push_batch(self, metric_name: str, metric_lines: list):
        """Push a batch of metrics to Pushgateway"""
        try:
            # Prometheus text format
            data = '\n'.join(metric_lines) + '\n'

            # Push to Pushgateway
            url = f"{self.pushgateway_url}/metrics/job/{self.namespace}/metric/{metric_name}"
            response = requests.post(
                url,
                data=data,
                headers={'Content-Type': 'text/plain'},
                timeout=10
            )

            if response.status_code not in [200, 202]:
                print(f"[Bulk Metric] Warning: Push failed for {metric_name}: {response.status_code}")

        except Exception as e:
            print(f"[Bulk Metric] Error pushing {metric_name}: {e}")
