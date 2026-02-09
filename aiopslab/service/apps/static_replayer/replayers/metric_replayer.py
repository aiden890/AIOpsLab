# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
Metric Replayer

Replays metric data in realtime (excluding history data).
Sends metrics to Prometheus Pushgateway with configurable speed factor.
"""

import time
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
from typing import Dict
import pandas as pd


class MetricReplayer:
    """Realtime metric replayer"""

    def __init__(self, adapter, pushgateway_url: str, namespace: str,
                 speed_factor: float, time_remapper):
        """
        Initialize metric replayer

        Args:
            adapter: Dataset adapter with load_metrics() method
            pushgateway_url: Prometheus Pushgateway URL
            namespace: Namespace/job name
            speed_factor: Replay speed (1.0 = realtime, 10.0 = 10x faster)
            time_remapper: TimeRemapper instance
        """
        self.adapter = adapter
        self.pushgateway_url = pushgateway_url.rstrip('/')
        self.namespace = namespace
        self.speed_factor = speed_factor
        self.time_remapper = time_remapper
        self.registry = CollectorRegistry()
        self.gauges: Dict[str, Gauge] = {}

    def replay(self):
        """Start realtime replay"""
        print(f"[Metric Replayer] Starting realtime replay...")
        print(f"  Speed factor: {self.speed_factor}x")
        print(f"  Namespace: {self.namespace}")

        start_time_real = time.time()
        anchor_original = self.time_remapper.mapping['anchor_original']
        last_push_time = 0
        metrics_replayed = 0

        for chunk in self.adapter.load_metrics():
            if chunk.empty:
                continue

            # Filter out history data (already bulk loaded)
            realtime_mask = ~chunk['timestamp'].apply(self.time_remapper.is_history)
            realtime_chunk = chunk[realtime_mask]

            if realtime_chunk.empty:
                continue

            for _, row in realtime_chunk.iterrows():
                # Calculate elapsed time in original timeline
                elapsed_data_time = row['timestamp'] - anchor_original
                elapsed_real_time = time.time() - start_time_real

                # Wait according to speed factor
                target_real_time = elapsed_data_time / self.speed_factor
                sleep_time = target_real_time - elapsed_real_time

                if sleep_time > 0:
                    time.sleep(sleep_time)

                # Update gauge with remapped timestamp
                simulation_ts = self.time_remapper.remap_timestamp(row['timestamp'])

                labels_dict = row['labels'].copy()
                labels_dict['is_history'] = 'false'
                labels_dict['namespace'] = self.namespace

                label_names = sorted(labels_dict.keys())
                label_values = [str(labels_dict[k]) for k in label_names]

                gauge = self._get_or_create_gauge(row['metric_name'], label_names)
                gauge.labels(*label_values).set(row['value'])

                metrics_replayed += 1

                # Push to Pushgateway periodically
                current_time = time.time()
                if current_time - last_push_time >= 1.0:  # Push every second
                    try:
                        push_to_gateway(
                            self.pushgateway_url,
                            job=self.namespace,
                            registry=self.registry
                        )
                        last_push_time = current_time
                    except Exception as e:
                        print(f"[Metric Replayer] Push error: {e}")

        # Final push
        try:
            push_to_gateway(
                self.pushgateway_url,
                job=self.namespace,
                registry=self.registry
            )
        except Exception as e:
            print(f"[Metric Replayer] Final push error: {e}")

        print(f"[Metric Replayer] ✓ Replay completed! {metrics_replayed} metrics replayed")

    def _get_or_create_gauge(self, metric_name: str, label_names: list) -> Gauge:
        """Get or create Prometheus Gauge"""
        # Sanitize metric name for Prometheus
        gauge_name = metric_name.replace('.', '_').replace('-', '_').replace('/', '_')

        if gauge_name not in self.gauges:
            self.gauges[gauge_name] = Gauge(
                gauge_name,
                f'Replayed metric: {metric_name}',
                label_names,
                registry=self.registry
            )

        return self.gauges[gauge_name]
