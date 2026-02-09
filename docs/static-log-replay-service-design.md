# Static Log Replay Service Design

This document describes the architecture for creating AIOpsLab-compatible service applications using static logs from the OpenRCA dataset (Market/cloudbed-1 and cloudbed-2).

---

## Overview

Instead of deploying real microservices with live fault injection, this design **replays pre-recorded telemetry data** (logs, metrics, traces) from the OpenRCA Market dataset into AIOpsLab's existing observability stack (Prometheus, Jaeger, kubectl logs). The agent experience is identical to debugging a live incident.

### Goals

- Use static OpenRCA dataset as the data source
- Deploy real Kubernetes pods that replay logs in real-time
- Backfill Prometheus and Jaeger with historical metrics/traces for instant availability
- Preserve the original AIOpsLab format (same APIs, same task types, same evaluation)
- Only add new files — minimal modification to existing AIOpsLab code
- Support both cloudbed-1 and cloudbed-2 as separate namespaces

### Non-Goals

- Mitigation task type (static data can't be "fixed")
- Telecom dataset (lacks logs, unsuitable for `get_logs()` API)
- Simultaneous deployment of both cloudbeds (run one at a time for RAM efficiency)

---

## Dataset Summary

### Market/cloudbed-1

| Data Type | File | Day Size | 90-min Window |
|-----------|------|----------|---------------|
| Application Logs | log_service.csv | 672 MB | ~42 MB |
| Proxy Logs | log_proxy.csv | 2.9 GB | ~180 MB |
| Container Metrics | metric_container.csv | 265 MB | ~17 MB |
| Node Metrics | metric_node.csv | 21 MB | ~1.3 MB |
| Service Metrics | metric_service.csv | 988 KB | ~62 KB |
| Mesh Metrics | metric_mesh.csv | 247 MB | ~15 MB |
| Runtime Metrics | metric_runtime.csv | 88 MB | ~5.5 MB |
| Traces | trace_span.csv | 1.3 GB | ~81 MB |
| Fault Records | record.csv | 70 records | - |

### Market/cloudbed-2

Similar structure with 78 fault records.

### Services (10 per cloudbed)

adservice, cartservice, checkoutservice, currencyservice, emailservice,
frontend, paymentservice, productcatalogservice, recommendationservice,
shippingservice (+ redis-cart)

---

## Architecture

### System Diagram

```
+-----------------------------------------------------------------------+
|                         Kubernetes Cluster                             |
|                                                                        |
|  +------------------------------------------------------------------+ |
|  |  NS: openrca-cloudbed-1 (or openrca-cloudbed-2)                  | |
|  |                                                                    | |
|  |  SERVICE PODS (one per service):                                  | |
|  |  +----------------+ +----------------+ +----------------+         | |
|  |  | adservice-0    | | cartservice-0  | | checkout-0     | ...     | |
|  |  | label: app=    | | label: app=    | | label: app=    |         | |
|  |  |  adservice     | |  cartservice   | | checkoutsvc    |         | |
|  |  |                | |                | |                |         | |
|  |  | [log-replayer] | | [log-replayer] | | [log-replayer] |         | |
|  |  |  Phase1 + 2    | |  Phase1 + 2    | |  Phase1 + 2    |         | |
|  |  |  -> stdout     | |  -> stdout     | |  -> stdout     |         | |
|  |  +-------+--------+ +-------+--------+ +-------+--------+         | |
|  |          | kubectl logs      |                  |                  | |
|  |          v                   v                  v                  | |
|  |      get_logs()          get_logs()         get_logs()            | |
|  |                                                                    | |
|  |  INFRASTRUCTURE PODS:                                             | |
|  |  +-------------------------------------------+                    | |
|  |  | Jaeger All-in-One                          |                    | |
|  |  |  Collector: :14268  Query: :16686          |                    | |
|  |  |  Contains: 90min of trace data (backfilled)|                    | |
|  |  +-------------------------------------------+                    | |
|  |                                                                    | |
|  |  BACKFILL JOBS (run once during deploy, then complete):           | |
|  |  +------------------+ +-------------------+                       | |
|  |  | Trace Backfill   | | Metric Backfill   |                       | |
|  |  | -> Jaeger:14268  | | -> Prometheus:32000|                       | |
|  |  +------------------+ +-------------------+                       | |
|  +------------------------------------------------------------------+ |
|                                                                        |
|  +------------------------------------------------------------------+ |
|  |  NS: observe (shared, already deployed by AIOpsLab)               | |
|  |                                                                    | |
|  |  +------------------------------------------------------------+  | |
|  |  | Prometheus Server                                           |  | |
|  |  |  NodePort: 32000                                            |  | |
|  |  |  Flags: --web.enable-remote-write-receiver                  |  | |
|  |  |         --storage.tsdb.out-of-order-time-window=90m         |  | |
|  |  |                                                             |  | |
|  |  |  Contains: 90min of metric data (backfilled instantly)      |  | |
|  |  +------------------------------------------------------------+  | |
|  +------------------------------------------------------------------+ |
+-----------------------------------------------------------------------+
```

---

## Timestamp Strategy: Current-Time Anchoring

When a scenario starts, all timestamps are remapped so the fault appears to have just happened.

```
DATASET TIMELINE (original):
    2022-03-20 08:00        09:09:06            10:00
    ----------------------------+---------------------
    [  normal operation  ] [FAULT] [ fault effects  ]

                    v  remap  v

REPLAYED TIMELINE (when agent runs):
    2026-02-09 14:00        15:09:06            16:00
    ----------------------------+---------------------
    [  normal operation  ] [FAULT] [ fault effects  ]
                                ^
                            ~ "now" when agent starts
```

### Implementation

```python
fault_record = records.iloc[scenario_id]
original_fault_time = fault_record['timestamp']  # 1647738546
time_offset = int(time.time()) - original_fault_time

# For any data point:
remapped_timestamp = original_timestamp + time_offset
```

### Data Window

For each scenario, load a 90-minute window:
- 60 minutes before the fault (baseline)
- 30 minutes after the fault (fault effects)

---

## Component Design

### 1. Log Replay (Per-Service Containers)

Each service pod runs a `log-replayer` container that outputs logs to stdout.

**Two-phase approach:**

| Phase | Time Range | Output Speed | Purpose |
|-------|-----------|--------------|---------|
| Phase 1 (Historical) | T-60min to T-5min | Fast (bulk dump) | Provides baseline context |
| Phase 2 (Real-time) | T-5min to T+30min | Real-time paced | Simulates live incident |

**Phase 2 pacing:** Each log line is output at its remapped timestamp. The script
sleeps between lines using `sleep(next_timestamp - current_time)`.

**Both log sources replayed per service:**
- `log_service.csv` — application logs (severity, message)
- `log_proxy.csv` — Envoy gateway logs (HTTP status, latency)

Interleaved by timestamp so the agent sees the full picture.

### 2. Metric Backfill (Prometheus Remote Write)

Pushes all metric data into Prometheus at deploy time for instant availability.

**Prometheus Configuration Changes:**

```yaml
server:
  extraFlags:
    - web.enable-lifecycle           # already present
    - web.enable-remote-write-receiver  # NEW
    - storage.tsdb.out-of-order-time-window=90m  # NEW
```

**Backfill job pushes data from 5 CSV sources:**

| Source | Prometheus Metric Names | Labels |
|--------|------------------------|--------|
| metric_container.csv | container_cpu_usage_seconds_total, container_memory_usage_bytes, container_network_*_total, ... | pod, namespace, interface |
| metric_node.csv | system_cpu_iowait, system_load_1, system_mem_*, ... | node, namespace |
| metric_service.csv | service_request_rate, service_success_rate, service_mean_response_time_ms | service, namespace |
| metric_mesh.csv | istio_requests_total, istio_request_duration_milliseconds, ... | pod, namespace |
| metric_runtime.csv | jvm_memory_bytes_used, jvm_gc_*, jvm_threads_* | service, namespace |

### 3. Trace Backfill (Jaeger Thrift HTTP)

Pushes all span data into Jaeger Collector at deploy time.

**Jaeger deployed per namespace** as `jaegertracing/all-in-one`.

**Push endpoint:** `POST http://jaeger:14268/api/traces` (Thrift HTTP)

**Trace data mapping:**

| Dataset Field | Jaeger Span Field |
|--------------|-------------------|
| timestamp (unix ms) | startTime (microseconds), remapped |
| cmdb_id | process.serviceName (extracted) |
| span_id | spanID |
| trace_id | traceID |
| duration (ms) | duration (microseconds) |
| status_code | tags: http.status_code |
| operation_name | operationName |
| parent_span | references: CHILD_OF |

### 4. Metric Name Mapping

Dataset metric names map systematically to Prometheus conventions:

```
RULE 1: Add _total suffix for counters
  container_cpu_usage_seconds     -> container_cpu_usage_seconds_total
  container_cpu_cfs_periods       -> container_cpu_cfs_periods_total

RULE 2: Convert MB to bytes (multiply by 1048576)
  container_memory_usage_MB       -> container_memory_usage_bytes
  container_memory_working_set_MB -> container_memory_working_set_bytes
  container_network_receive_MB    -> container_network_receive_bytes_total
  container_network_transmit_MB   -> container_network_transmit_bytes_total

RULE 3: Extract interface suffix to label
  container_network_receive_packets.eth0
    -> container_network_receive_packets_total{interface="eth0"}

RULE 4: Map cmdb_id to pod + namespace labels
  "node-6.adservice2-0"
    -> {pod="adservice2-0", namespace="openrca-cloudbed-1", node="node-6"}

RULE 5: Keep as-is (already matching)
  container_memory_cache, container_memory_rss, container_threads, etc.
```

---

## Fault Injection: Static Scenario Selection

Instead of injecting live faults, select a pre-recorded fault scenario.

```python
class StaticFaultInjector(FaultInjector):
    def _inject(self, scenario_id):
        record = self.records.iloc[scenario_id]
        self.active_fault = record
        # Ground truth: {level, component, reason, timestamp}

    def _recover(self):
        self.active_fault = None
```

### Fault-to-AIOpsLab Mapping

| Dataset Reason | system_level | fault_type |
|---------------|-------------|------------|
| container CPU load | Virtualization | Operation Error |
| container memory load | Virtualization | Operation Error |
| container read I/O load | Hardware | Network/Storage Issue |
| container write I/O load | Hardware | Network/Storage Issue |
| container network latency | Virtualization | Network/Storage Issue |
| container packet loss | Virtualization | Network/Storage Issue |
| container packet corruption | Virtualization | Network/Storage Issue |
| container packet retransmission | Virtualization | Network/Storage Issue |
| container process termination | Virtualization | Dependency Problem |
| node memory consumption | Hardware | Operation Error |
| node CPU load | Hardware | Operation Error |
| node CPU spike | Hardware | Operation Error |
| node disk I/O consumption | Hardware | Network/Storage Issue |
| node disk space consumption | Hardware | Network/Storage Issue |

### Problem Count

- cloudbed-1: 70 faults x 3 task types = 210 problems
- cloudbed-2: 78 faults x 3 task types = 234 problems
- Total: 444 benchmark scenarios

Task types supported: Detection, Localization, Analysis (no Mitigation).

---

## Deployment Sequence

```
OpenRCAService.deploy(scenario_id):

  STEP 1: Scenario Setup (instant)
    - Load record.csv, select fault
    - Calculate time_offset = now() - fault_timestamp
    - Define 90-minute window

  STEP 2: Data Preprocessing (5-15 seconds)
    - Filter all CSVs to window
    - Apply metric name mapping
    - Remap timestamps
    - Generate per-service log files

  STEP 3: Create Namespace + Deploy Jaeger (~10 seconds)
    - kubectl create namespace
    - Deploy jaeger all-in-one
    - Wait for ready

  STEP 4: Backfill Storage (10-30 seconds, parallel)
    - Metric backfill job -> Prometheus (remote_write)
    - Trace backfill job -> Jaeger (Thrift HTTP)
    - Both run as K8s Jobs, complete in parallel

  STEP 5: Deploy Service Pods (~15 seconds)
    - Deploy per-service pods with log-replayer containers
    - Mount preprocessed log data
    - Phase 1 (bulk dump) completes during pod startup
    - Phase 2 (real-time pacing) begins

  STEP 6: Ready (~40-60 seconds total)
    - Prometheus: all metrics instantly queryable
    - Jaeger: all traces instantly queryable
    - Pods: running, logs flowing
    - Return to orchestrator -> agent begins
```

---

## AIOpsLab Integration

### New Files (additive only)

```
aiopslab/
  service/
    apps/
      openrca.py                    # OpenRCAService (Application subclass)
    metadata/
      openrca_cloudbed1.json        # Service metadata
      openrca_cloudbed2.json
  generators/
    fault/
      inject_static.py              # StaticFaultInjector
  orchestrator/
    problems/
      openrca_cloudbed1/            # Problem definitions (70 faults)
      openrca_cloudbed2/            # Problem definitions (78 faults)

# Docker image
openrca-replayer/
  Dockerfile
  log_replayer.py                   # Container entrypoint
  metric_backfill.py                # K8s Job entrypoint
  trace_backfill.py                 # K8s Job entrypoint
  preprocess.py                     # Metric name mapping + filters

# Kubernetes manifests
k8s/openrca/
  service-pod-template.yaml
  jaeger-deployment.yaml
  backfill-jobs.yaml
```

### Existing Code Changes (minimal)

**1. Label selector mapping** (2 lines in `aiopslab/orchestrator/actions/base.py`):

```python
elif namespace in ("openrca-cloudbed-1", "openrca-cloudbed-2"):
    selector = f"app={service}"
```

**2. Prometheus config** (2 extra flags in Helm values):

```yaml
server:
  extraFlags:
    - web.enable-remote-write-receiver
    - storage.tsdb.out-of-order-time-window=90m
```

**3. Problem registry** (add entries for new problems).

---

## Resource Requirements

### Storage

| Component | Size |
|-----------|------|
| openrca_dataset (already on disk) | 47 GB |
| Docker image (openrca-replayer) | ~300 MB |
| Preprocessed data per scenario | ~50 MB (temp) |
| Prometheus PVC | 8 GB (existing) |
| K8s system overhead | ~5-10 GB |
| **Total NEW storage** | **~10-15 GB** |

### RAM (per run, one cloudbed at a time)

| Component | RAM |
|-----------|-----|
| Kubernetes base | ~2-3 GB |
| Prometheus | ~500 MB |
| Jaeger (per namespace) | ~200-400 MB |
| 11 service pods | ~550 MB |
| Backfill jobs (temporary) | ~500 MB |
| **Total K8s** | **~4-5 GB** |

Recommendation: Run one cloudbed at a time (not both simultaneously).

---

## Agent Experience

The agent sees an experience identical to a live service:

```
>>> exec_shell("kubectl get pods -n openrca-cloudbed-1")
NAME                  READY   STATUS    RESTARTS   AGE
adservice-0           1/1     Running   0          2m
cartservice-0         1/1     Running   0          2m
...
shippingservice-0     1/1     Running   0          2m

>>> get_logs("openrca-cloudbed-1", "shippingservice")
severity: info, message: shipment processed successfully
severity: warn, message: high I/O wait detected
severity: error, message: read timeout on disk operation    <-- fault!

>>> get_metrics("openrca-cloudbed-1", 10)
Metrics exported to: metrics_output/metric_20260209_150500/

>>> get_traces("openrca-cloudbed-1", 5)
Trace data exported to: trace_output/traces_1707483900.csv

>>> submit(["shippingservice-1"])   # Localization answer
```

---

## Open Decisions

1. **Docker image registry**: Build locally or push to a registry?
2. **Data mounting**: ConfigMap (small data) vs PersistentVolume (large log files)?
   - Recommendation: PV for log data, ConfigMap for config
3. **Scenario selection**: Pass scenario_id at deploy time or have a separate config?
4. **Multiple pods per service**: Dataset has adservice-0, adservice2-0 — deploy both or just one?
