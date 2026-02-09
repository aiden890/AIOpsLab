"""OpenRCA Service Application for static log replay.

Supports all OpenRCA datasets: Market (cloudbed-1/2), Telecom, Bank.

Instead of deploying real microservices, this replays pre-recorded telemetry data
into Kubernetes pods (logs), Prometheus (metrics), and Jaeger (traces).
"""

import os
import time
import json
import struct
import tempfile

import pandas as pd
import requests

from aiopslab.service.kubectl import KubeCtl
from aiopslab.service.apps.base import Application
from aiopslab.service.apps.openrca.preprocess import (
    load_records,
    get_fault_info,
    compute_time_offset,
    find_telemetry_day,
    prepare_logs_for_service,
    prepare_metrics,
    prepare_traces,
)
from aiopslab.paths import OPENRCA_DATASET_DIR


class OpenRCAService(Application):
    """Application that replays static OpenRCA dataset telemetry."""

    def __init__(self, config_file: str, scenario_id: int = 0):
        super().__init__(config_file)
        self.kubectl = KubeCtl()
        self.scenario_id = scenario_id
        self.helm_deploy = False

        self.load_app_json()
        self._load_scenario()

    def load_app_json(self):
        super().load_app_json()
        metadata = self.get_app_json()
        self.services = metadata.get("Services", [])
        self.dataset_type = metadata.get("Dataset Type", "market")
        self.has_logs = metadata.get("Has Logs", True)
        self.trace_format = metadata.get("Trace Format", "market")
        self.metric_files = metadata.get("Metric Files", [])

        # Resolve dataset path
        dataset_rel_path = metadata.get("Dataset Path", "")
        self.dataset_path = str(OPENRCA_DATASET_DIR.parent / dataset_rel_path)

    def set_scenario(self, scenario_id: int):
        """Set the scenario ID and reload scenario data."""
        self.scenario_id = scenario_id
        self._load_scenario()

    def _load_scenario(self):
        """Load fault record and compute time offset for the scenario."""
        self.records = load_records(self.dataset_path)
        record = self.records.iloc[self.scenario_id]
        self.fault_info = get_fault_info(record, self.dataset_type)
        self.fault_timestamp = self.fault_info["timestamp"]
        self.time_offset = compute_time_offset(self.fault_timestamp)
        self.telemetry_day = find_telemetry_day(self.dataset_path, self.fault_timestamp)

    def deploy(self):
        """Deploy the OpenRCA static replay environment.

        Steps:
        1. Create namespace
        2. Deploy Jaeger all-in-one for trace storage
        3. Backfill metrics into Prometheus
        4. Backfill traces into Jaeger
        5. Deploy log replayer pods (if dataset has logs)
        """
        print(f"[OpenRCA] Deploying scenario {self.scenario_id} "
              f"({self.dataset_type}) in namespace {self.namespace}")
        print(f"  Fault: {self.fault_info['reason']} on {self.fault_info['component']}")
        print(f"  Original time: {self.fault_info['datetime']}")

        # Recalculate time offset at deploy time
        self.time_offset = compute_time_offset(self.fault_timestamp)

        # Step 1: Create namespace
        self.create_namespace()

        # Step 2: Deploy Jaeger
        self._deploy_jaeger()

        # Step 3: Backfill metrics into Prometheus
        self._backfill_metrics()

        # Step 4: Backfill traces into Jaeger
        self._backfill_traces()

        # Step 5: Deploy log replayer pods
        if self.has_logs:
            self._deploy_log_replayers()

        print(f"[OpenRCA] Deployment complete. Namespace: {self.namespace}")

    def delete(self):
        """Delete all resources in the namespace."""
        self.kubectl.delete_namespace(self.namespace)

    def cleanup(self):
        """Delete the entire namespace."""
        self.kubectl.delete_namespace(self.namespace)
        time.sleep(5)

    # ----- Jaeger deployment -----

    def _deploy_jaeger(self):
        """Deploy Jaeger all-in-one in the namespace."""
        print("[OpenRCA] Deploying Jaeger...")
        jaeger_manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "jaeger",
                "namespace": self.namespace,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "jaeger"}},
                "template": {
                    "metadata": {"labels": {"app": "jaeger"}},
                    "spec": {
                        "containers": [{
                            "name": "jaeger",
                            "image": "jaegertracing/all-in-one:1.57",
                            "ports": [
                                {"containerPort": 16686, "name": "query"},
                                {"containerPort": 14268, "name": "collector"},
                            ],
                            "env": [
                                {"name": "COLLECTOR_OTLP_ENABLED", "value": "true"},
                            ],
                            "resources": {
                                "requests": {"memory": "200Mi", "cpu": "100m"},
                                "limits": {"memory": "400Mi", "cpu": "500m"},
                            },
                        }],
                    },
                },
            },
        }

        jaeger_service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "jaeger",
                "namespace": self.namespace,
            },
            "spec": {
                "selector": {"app": "jaeger"},
                "ports": [
                    {"name": "query", "port": 16686, "targetPort": 16686},
                    {"name": "collector", "port": 14268, "targetPort": 14268},
                ],
            },
        }

        self._apply_manifest(jaeger_manifest)
        self._apply_manifest(jaeger_service)

        # Wait for Jaeger to be ready
        self.kubectl.exec_command(
            f"kubectl wait --for=condition=available deployment/jaeger "
            f"-n {self.namespace} --timeout=60s"
        )
        print("[OpenRCA] Jaeger deployed.")

    # ----- Metric backfill -----

    def _backfill_metrics(self):
        """Backfill metrics into Prometheus via remote_write API."""
        print("[OpenRCA] Backfilling metrics into Prometheus...")

        timeseries = prepare_metrics(
            self.dataset_path,
            self.fault_timestamp,
            self.time_offset,
            self.namespace,
            day=self.telemetry_day,
            dataset_type=self.dataset_type,
        )

        if not timeseries:
            print("[OpenRCA] No metrics to backfill.")
            return

        # Port-forward to Prometheus server
        local_port = 9090
        pf_cmd = (
            f"kubectl port-forward svc/prometheus-server -n observe "
            f"{local_port}:80 &"
        )
        os.system(pf_cmd)
        time.sleep(3)

        prometheus_url = f"http://localhost:{local_port}/api/v1/write"

        # Send in batches
        batch_size = 100
        total = len(timeseries)
        sent = 0
        try:
            for i in range(0, total, batch_size):
                batch = timeseries[i:i + batch_size]
                payload = self._build_remote_write_payload(batch)
                headers = {
                    "Content-Type": "application/x-protobuf",
                    "X-Prometheus-Remote-Write-Version": "0.1.0",
                }

                compressed = self._snappy_compress(payload)
                if compressed is not None:
                    headers["Content-Encoding"] = "snappy"
                    resp = requests.post(prometheus_url, data=compressed, headers=headers)
                else:
                    resp = requests.post(prometheus_url, data=payload, headers=headers)

                if resp.status_code not in (200, 204):
                    print(f"[OpenRCA] Metric backfill batch {i//batch_size}: "
                          f"HTTP {resp.status_code} - {resp.text[:200]}")
                else:
                    sent += len(batch)
        except requests.exceptions.ConnectionError:
            print("[OpenRCA] Warning: Cannot connect to Prometheus at "
                  f"{prometheus_url}. Skipping remaining metric backfill.")
        finally:
            # Kill port-forward
            os.system("pkill -f 'port-forward svc/prometheus-server'")

        print(f"[OpenRCA] Backfilled {sent}/{total} metric timeseries.")

    def _build_remote_write_payload(self, timeseries_batch: list) -> bytes:
        """Build Prometheus remote_write protobuf payload.

        Uses manual protobuf encoding to avoid requiring the prometheus_pb2 dependency.
        Format: WriteRequest { repeated TimeSeries timeseries = 1; }
        """
        ts_data = b""
        for ts in timeseries_batch:
            labels = ts["labels"]
            samples = ts["samples"]

            # Encode labels
            labels_data = b""
            for name, value in sorted(labels.items()):
                # Label { string name = 1; string value = 2; }
                label_data = (
                    self._encode_field(1, name.encode()) +
                    self._encode_field(2, str(value).encode())
                )
                labels_data += self._encode_field(1, label_data)

            # Encode samples
            samples_data = b""
            for ts_ms, value in samples:
                # Sample { double value = 1; int64 timestamp = 2; }
                sample_data = (
                    self._encode_double(1, value) +
                    self._encode_varint_field(2, ts_ms)
                )
                samples_data += self._encode_field(2, sample_data)

            ts_data += self._encode_field(1, labels_data + samples_data)

        return ts_data

    @staticmethod
    def _encode_field(field_number: int, data: bytes) -> bytes:
        """Encode a length-delimited protobuf field."""
        tag = (field_number << 3) | 2
        return OpenRCAService._encode_varint(tag) + OpenRCAService._encode_varint(len(data)) + data

    @staticmethod
    def _encode_double(field_number: int, value: float) -> bytes:
        """Encode a double protobuf field."""
        tag = (field_number << 3) | 1
        return OpenRCAService._encode_varint(tag) + struct.pack("<d", value)

    @staticmethod
    def _encode_varint_field(field_number: int, value: int) -> bytes:
        """Encode a varint protobuf field."""
        tag = (field_number << 3) | 0
        return OpenRCAService._encode_varint(tag) + OpenRCAService._encode_varint(value)

    @staticmethod
    def _snappy_compress(data: bytes) -> bytes:
        """Snappy-compress data for Prometheus remote_write.

        Tries cramjam (pure pip install), then python-snappy.
        Returns None if no snappy library is available.
        """
        try:
            import cramjam
            return bytes(cramjam.snappy.compress_raw(data))
        except ImportError:
            pass
        try:
            import snappy
            return snappy.compress(data)
        except ImportError:
            print("[OpenRCA] Error: No snappy library available. "
                  "Install one: pip install cramjam")
            return None

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        """Encode an integer as a protobuf varint."""
        result = b""
        while value > 0x7F:
            result += bytes([(value & 0x7F) | 0x80])
            value >>= 7
        result += bytes([value & 0x7F])
        return result

    # ----- Trace backfill -----

    def _backfill_traces(self):
        """Backfill trace data into Jaeger via Thrift HTTP collector."""
        print("[OpenRCA] Backfilling traces into Jaeger...")

        df_traces = prepare_traces(
            self.dataset_path,
            self.fault_timestamp,
            self.time_offset,
            self.namespace,
            day=self.telemetry_day,
            dataset_type=self.dataset_type,
        )

        if df_traces.empty:
            print("[OpenRCA] No traces to backfill.")
            return

        # Port-forward to Jaeger collector
        # Use kubectl port-forward in background
        pf_cmd = (
            f"kubectl port-forward svc/jaeger -n {self.namespace} 14268:14268 &"
        )
        os.system(pf_cmd)
        time.sleep(2)

        jaeger_url = "http://localhost:14268/api/traces"

        # Send traces in batches grouped by service
        batch_count = 0
        for service_name, group in df_traces.groupby("service_name"):
            spans = []
            for _, row in group.iterrows():
                span = {
                    "traceIdHigh": 0,
                    "traceIdLow": hash(str(row.get("trace_id", ""))) & 0xFFFFFFFFFFFFFFFF,
                    "spanId": hash(str(row.get("span_id", ""))) & 0xFFFFFFFFFFFFFFFF,
                    "operationName": str(row.get("operation_name", "unknown")),
                    "startTime": int(row.get("remapped_timestamp", 0)) * 1000,  # to microseconds
                    "duration": int(float(row.get("duration", 0)) * 1000),  # to microseconds
                    "tags": [],
                    "logs": [],
                    "references": [],
                }

                # Add parent reference
                parent = row.get("parent_span", "")
                if pd.notna(parent) and str(parent).strip():
                    span["references"].append({
                        "refType": "CHILD_OF",
                        "traceIdHigh": 0,
                        "traceIdLow": hash(str(row.get("trace_id", ""))) & 0xFFFFFFFFFFFFFFFF,
                        "spanId": hash(str(parent)) & 0xFFFFFFFFFFFFFFFF,
                    })

                spans.append(span)

            if spans:
                payload = {
                    "process": {
                        "serviceName": service_name,
                        "tags": [
                            {"key": "namespace", "vType": "STRING", "vStr": self.namespace},
                        ],
                    },
                    "spans": spans[:1000],  # Limit batch size
                }

                try:
                    resp = requests.post(
                        jaeger_url,
                        json={"batch": payload},
                        headers={"Content-Type": "application/json"},
                    )
                    batch_count += 1
                except requests.exceptions.ConnectionError:
                    print("[OpenRCA] Warning: Cannot connect to Jaeger. "
                          "Skipping trace backfill.")
                    break

        # Kill port-forward
        os.system("pkill -f 'port-forward svc/jaeger'")

        print(f"[OpenRCA] Backfilled {batch_count} trace batches.")

    # ----- Log replayer pods -----

    def _deploy_log_replayers(self):
        """Deploy log replayer pods for each service."""
        print("[OpenRCA] Deploying log replayer pods...")

        for service_name in self.services:
            # Prepare log data for this service
            log_df = prepare_logs_for_service(
                self.dataset_path,
                service_name,
                self.fault_timestamp,
                self.time_offset,
                day=self.telemetry_day,
                dataset_type=self.dataset_type,
            )

            if log_df.empty:
                # Deploy empty pod (still needed for kubectl get pods)
                self._deploy_stub_pod(service_name)
                continue

            # Build log lines, truncate if too large for ConfigMap (~900KB limit)
            log_lines = []
            for _, row in log_df.iterrows():
                ts = row["remapped_timestamp"]
                log_lines.append(f"{ts}|{row['value']}")

            log_data = "\n".join(log_lines)

            max_cm_size = 900_000  # ~900KB to stay under K8s API 1MB ConfigMap limit
            data_size = len(log_data.encode("utf-8"))
            if data_size > max_cm_size:
                # Estimate how many lines to keep, keep newest (closest to fault)
                avg_line_size = data_size / len(log_lines)
                keep_lines = int(max_cm_size / avg_line_size * 0.9)  # 10% margin
                log_lines = log_lines[-keep_lines:]
                log_data = "\n".join(log_lines)
                print(f"[OpenRCA] Truncated logs for {service_name}: "
                      f"{keep_lines} lines ({len(log_data)//1024}KB)")

            # Create ConfigMap with log data
            cm_name = f"{service_name}-logs"
            self.kubectl.create_or_update_configmap(
                name=cm_name,
                namespace=self.namespace,
                data={"logs.txt": log_data},
            )

            # Deploy pod with log replayer
            self._deploy_replayer_pod(service_name, cm_name)

        print(f"[OpenRCA] Deployed {len(self.services)} service pods.")

    def _deploy_stub_pod(self, service_name: str):
        """Deploy a minimal pod that just runs (for services without logs)."""
        pod_manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"{service_name}-0",
                "namespace": self.namespace,
                "labels": {"app": service_name},
            },
            "spec": {
                "containers": [{
                    "name": "stub",
                    "image": "busybox:1.36",
                    "command": ["sh", "-c", "echo 'Service stub running' && sleep infinity"],
                    "resources": {
                        "requests": {"memory": "16Mi", "cpu": "10m"},
                        "limits": {"memory": "32Mi", "cpu": "50m"},
                    },
                }],
            },
        }
        self._apply_manifest(pod_manifest)

    def _deploy_replayer_pod(self, service_name: str, configmap_name: str):
        """Deploy a pod that replays logs from ConfigMap to stdout."""
        # The replayer script reads timestamps and paces output in real-time
        replayer_script = r"""
import sys, time

log_file = '/data/logs.txt'
try:
    with open(log_file, 'r') as f:
        lines = f.readlines()
except FileNotFoundError:
    print('No log data available', flush=True)
    while True:
        time.sleep(3600)

if not lines:
    print('Empty log file', flush=True)
    while True:
        time.sleep(3600)

now = time.time()

# Phase 1: Bulk dump historical logs (before now - 5min)
phase2_start = now - 300
for line in lines:
    parts = line.strip().split('|', 1)
    if len(parts) != 2:
        continue
    ts, msg = float(parts[0]), parts[1]
    if ts < phase2_start:
        print(msg, flush=True)
    else:
        break

# Phase 2: Real-time paced replay
for line in lines:
    parts = line.strip().split('|', 1)
    if len(parts) != 2:
        continue
    ts, msg = float(parts[0]), parts[1]
    if ts < phase2_start:
        continue
    sleep_until = ts
    wait = sleep_until - time.time()
    if wait > 0:
        time.sleep(min(wait, 60))
    print(msg, flush=True)

# Keep pod alive after replay
while True:
    time.sleep(3600)
"""

        pod_manifest = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": f"{service_name}-0",
                "namespace": self.namespace,
                "labels": {"app": service_name},
            },
            "spec": {
                "containers": [{
                    "name": "log-replayer",
                    "image": "python:3.11-slim",
                    "command": ["python", "-c", replayer_script],
                    "volumeMounts": [{
                        "name": "log-data",
                        "mountPath": "/data",
                    }],
                    "resources": {
                        "requests": {"memory": "32Mi", "cpu": "10m"},
                        "limits": {"memory": "64Mi", "cpu": "100m"},
                    },
                }],
                "volumes": [{
                    "name": "log-data",
                    "configMap": {"name": configmap_name},
                }],
            },
        }
        self._apply_manifest(pod_manifest)

    # ----- Helpers -----

    def _apply_manifest(self, manifest: dict):
        """Apply a Kubernetes manifest using kubectl."""
        manifest_json = json.dumps(manifest)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(manifest_json)
            f.flush()
            result = self.kubectl.exec_command(f"kubectl apply -f {f.name}")
            os.unlink(f.name)
        return result
