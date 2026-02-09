# AcmeTrace GPU Cluster Service Design

This document describes the tasks needed to integrate the AcmeTrace GPU cluster dataset
(Shanghai AI Lab, NSDI'24) into AIOpsLab as a static replay service.

**Prerequisite**: Implement Market/OpenRCA static log replay first. The patterns established
there (Prometheus backfill, timestamp anchoring, static fault injection) carry over directly.

---

## Overview

AcmeTrace contains 6 months of GPU cluster traces from two clusters (Seren, Kalos) with
4,704 A100 GPUs. Unlike the microservice-oriented Market/OpenRCA dataset, this is a
**GPU HPC cluster** with fundamentally different telemetry and failure modes.

### Key Differences from Market/OpenRCA

| Aspect | Market/OpenRCA | AcmeTrace |
|--------|---------------|-----------|
| Architecture | Microservices (10 pods) | GPU cluster (200+ nodes, 4704 GPUs) |
| Data format | Long format (row per metric) | Wide format (column per node) |
| Logs | Application + Proxy logs | NONE |
| Traces | Distributed spans | NONE |
| GPU metrics | None | 15 DCGM/IPMI metrics |
| Power monitoring | None | GPU + CPU power (IPMI) |
| Failure types | Container/Node faults | Job failures (NVLink, OOM, NCCL, etc.) |
| Scale per scenario | ~50 MB (90-min window) | ~200 MB (90-min window, Kalos) |

---

## Task List

### Phase 1: Data Layer (Foundation)

#### Task 1.1: Wide-to-Long Format Transformer

The AcmeTrace CSVs are in **wide format** (one column per node). AIOpsLab and Prometheus
expect **long format** (one row per metric per time).

**Input (AcmeTrace GPU_UTIL.csv):**
```
Time,                      10.140.1.10, 10.140.1.54, 10.140.1.90, ...
2023-07-01 08:00:00+08:00, 85.2,        92.1,        0.0,         ...
2023-07-01 08:00:15+08:00, 86.1,        91.8,        0.0,         ...
```

**Output (Prometheus-compatible long format):**
```
timestamp, node, gpu_id, metric_name, value
1688169600, 10.140.1.10, gpu0, dcgm_gpu_utilization, 85.2
1688169600, 10.140.1.54, gpu0, dcgm_gpu_utilization, 92.1
```

**Work needed:**
- Python preprocessing script to pivot wide CSVs to long format
- Handle per-GPU vs per-node metrics (GPU metrics have 8 values per node)
- Memory-efficient processing (some files are 6+ GB)
- Output format compatible with Prometheus remote_write backfill

**Estimated effort:** Medium

---

#### Task 1.2: GPU Metric Name Mapping

Map AcmeTrace metric names to DCGM Exporter / Prometheus conventions.

| AcmeTrace File | Prometheus Metric Name | Type | Unit |
|---------------|----------------------|------|------|
| GPU_UTIL.csv | `DCGM_FI_DEV_GPU_UTIL` | gauge | % |
| GPU_TEMP.csv | `DCGM_FI_DEV_GPU_TEMP` | gauge | celsius |
| MEMORY_TEMP.csv | `DCGM_FI_DEV_MEMORY_TEMP` | gauge | celsius |
| FB_USED.csv | `DCGM_FI_DEV_FB_USED` | gauge | MB |
| FB_FREE.csv | `DCGM_FI_DEV_FB_FREE` | gauge | MB |
| MEM_CLOCK.csv | `DCGM_FI_DEV_MEM_CLOCK` | gauge | MHz |
| MEM_COPY_UTIL.csv | `DCGM_FI_DEV_MEM_COPY_UTIL` | gauge | % |
| SM_ACTIVE.csv | `DCGM_FI_PROF_SM_ACTIVE` | gauge | ratio |
| SM_OCCUPANCY.csv | `DCGM_FI_PROF_SM_OCCUPANCY` | gauge | ratio |
| PIPE_TENSOR_ACTIVE.csv | `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | gauge | ratio |
| POWER_USAGE.csv | `DCGM_FI_DEV_POWER_USAGE` | gauge | watts |
| XID_ERRORS.csv | `DCGM_FI_DEV_XID_ERRORS` | gauge | error_code |
| DRAM_ACTIVE.csv | `DCGM_FI_PROF_DRAM_ACTIVE` | gauge | ratio |
| NODE_CPU_UTILIZATION.csv | `node_cpu_utilization_percent` | gauge | % |
| NODE_MEMORY_UTILIZATION.csv | `node_memory_utilization_percent` | gauge | % |
| NODE_IB_RECEIVE.csv | `node_infiniband_receive_bytes_total` | counter | bytes |
| NODE_IB_SEND.csv | `node_infiniband_transmit_bytes_total` | counter | bytes |

**IPMI Power metrics:**

| AcmeTrace File | Prometheus Metric Name | Unit |
|---------------|----------------------|------|
| GPU_AB_Power.csv | `ipmi_gpu_power_watts{model="AB"}` | watts |
| GPU_C_Power.csv | `ipmi_gpu_power_watts{model="C"}` | watts |
| CPU_D_Power.csv | `ipmi_cpu_power_watts{model="D"}` | watts |

**Labels for all metrics:**
```
{node="10.140.1.10", gpu="gpu0", namespace="acmetrace-kalos", cluster="kalos"}
```

**Work needed:**
- Mapping table (Python dict or YAML config)
- Unit conversion where needed
- Per-GPU label assignment (8 GPUs per node for A100 nodes)

**Estimated effort:** Low

---

#### Task 1.3: Job Trace Preprocessor

Parse job traces to create fault scenarios and ground truth.

**Input:** `trace_kalos.csv` (or `trace_seren.csv`)
```
job_id, user, node_num, gpu_num, state, submit_time, start_time, end_time, duration
dlctk696, uf794, 8, 64, FAILED, 2023-05-17 11:00:58, ..., ..., 18
```

**Output:** Scenario definitions (similar to Market record.csv)
```
scenario_id, job_id, state, start_time, end_time, duration, gpu_num, node_num,
category, reason, affected_node, affected_gpu, xid_count
```

**Work needed:**
- Filter failed jobs (FAILED, NODE_FAIL, TIMEOUT)
- Cross-reference with XID_ERRORS.csv for hardware fault correlation
- Assign ground truth labels using the error taxonomy from the NSDI paper:
  - Infrastructure: NVLink, CUDA, ECC, Node Failure, Network, NCCL
  - Framework: OOM, Dataloader Killed, Runtime Error
  - Script: FileNotFound, TypeError, ImportError
- Use the existing `sample_kalos_rca.py` as a starting point
- Generate detection/localization/analysis problem definitions

**Estimated effort:** Medium

---

### Phase 2: New Telemetry APIs

#### Task 2.1: GPU Metrics API

New action for querying GPU-specific metrics.

```python
@read
@action
def get_gpu_metrics(namespace: str, node: str = None, duration: int = 5) -> str:
    """
    Collect GPU metrics (DCGM) from Prometheus.

    Returns CSV with: timestamp, node, gpu_id, metric_name, value

    Metrics: GPU_UTIL, GPU_TEMP, FB_USED, FB_FREE, SM_ACTIVE,
             SM_OCCUPANCY, PIPE_TENSOR_ACTIVE, MEM_COPY_UTIL,
             MEM_CLOCK, MEMORY_TEMP, POWER_USAGE, XID_ERRORS
    """
```

**Work needed:**
- New method in actions class (extends TaskActions)
- PromQL queries for DCGM metric names
- CSV export in standard format
- Optional node filter (query all nodes or specific node)

**Estimated effort:** Medium

---

#### Task 2.2: Power Metrics API

New action for querying power/energy metrics.

```python
@read
@action
def get_power_metrics(namespace: str, node: str = None, duration: int = 5) -> str:
    """
    Collect power metrics (IPMI) from Prometheus.

    Returns CSV with: timestamp, node, component, power_watts

    Metrics: GPU power (per model), CPU power
    """
```

**Work needed:**
- New method in actions class
- PromQL queries for IPMI metric names
- CSV export

**Estimated effort:** Low

---

#### Task 2.3: Job Info API

New action for querying job metadata.

```python
@read
@action
def get_job_info(job_id: str = None, state: str = None) -> str:
    """
    Get job information from the cluster scheduler.

    Returns: job_id, user, node_num, gpu_num, state, submit_time,
             start_time, end_time, duration, queue_time
    """
```

**Work needed:**
- Read from preprocessed job trace CSV
- Filter by job_id, state, or time range
- Format as structured string or CSV

**Estimated effort:** Low

---

#### Task 2.4: XID Error API

New action for querying GPU hardware errors.

```python
@read
@action
def get_xid_errors(namespace: str, node: str = None, duration: int = 5) -> str:
    """
    Get GPU XID error codes from the cluster.

    Returns CSV with: timestamp, node, gpu_id, xid_code, description

    Common XID codes:
      43 - GPU stopped processing (NVLink Error)
      48 - Double Bit ECC Error
      63 - ECC page retirement
      79 - GPU has fallen off the bus
    """
```

**Work needed:**
- Parse XID_ERRORS.csv (non-zero values indicate errors)
- Map XID codes to descriptions
- PromQL query or direct CSV serving

**Estimated effort:** Low

---

#### Task 2.5: Node Metrics API (enhanced)

The existing `get_metrics()` queries container-level metrics. For GPU clusters,
node-level metrics are more important.

```python
@read
@action
def get_node_metrics(namespace: str, node: str = None, duration: int = 5) -> str:
    """
    Collect node-level metrics from Prometheus.

    Returns CSV with: timestamp, node, metric_name, value

    Metrics: CPU utilization, Memory utilization,
             InfiniBand receive/transmit bytes
    """
```

**Work needed:**
- New method in actions class
- PromQL queries for node-level metrics
- InfiniBand metrics (critical for distributed training debugging)

**Estimated effort:** Low

---

### Phase 3: Application & Service Layer

#### Task 3.1: AcmeTraceService (Application Subclass)

New Application class for the GPU cluster.

```python
class AcmeTraceService(Application):
    """AcmeTrace GPU cluster static replay service."""

    def __init__(self, cluster="kalos"):
        # Load cluster metadata
        # cluster = "kalos" (20K jobs) or "seren" (664K jobs)

    def deploy(self, scenario_id):
        # 1. Create namespace (acmetrace-kalos or acmetrace-seren)
        # 2. Preprocess utilization CSVs for scenario window
        # 3. Backfill Prometheus with GPU/node/power metrics
        # 4. Deploy stub pods representing cluster nodes
        # 5. (Optional) Deploy stub pods for job workers

    def cleanup(self):
        # Delete namespace and clean up
```

**Work needed:**
- Application subclass following existing pattern
- Metadata JSON files for kalos and seren clusters
- Namespace management
- Deployment orchestration (reuse Prometheus backfill from OpenRCA)

**Estimated effort:** Medium

---

#### Task 3.2: GPU Stub Pods

Deploy lightweight pods representing cluster nodes (not microservices).

```yaml
# Per physical node, deploy a stub pod:
apiVersion: v1
kind: Pod
metadata:
  name: node-10-140-1-10
  namespace: acmetrace-kalos
  labels:
    app: gpu-node
    node-ip: "10.140.1.10"
    gpu-count: "8"
    gpu-type: "A100"
spec:
  containers:
    - name: node-stub
      image: openrca-replayer:latest  # reuse same image
      command: ["python", "/app/gpu_node_stub.py"]
      env:
        - name: NODE_IP
          value: "10.140.1.10"
        - name: SCENARIO_ID
          value: "42"
```

**What the stub does:**
- Outputs synthesized "node event logs" to stdout (job started, job failed, GPU error detected)
- These are NOT real application logs — they're synthesized from job trace + XID data
- Provides a target for `kubectl logs` and `kubectl describe pod`

**Work needed:**
- Pod template YAML
- Node stub script (synthesize event logs from job metadata)
- Label scheme for node selection

**Estimated effort:** Medium

---

#### Task 3.3: GPUFaultInjector (Static Fault Selection)

```python
class GPUFaultInjector(FaultInjector):
    """Selects a GPU failure scenario from AcmeTrace dataset."""

    def _inject(self, scenario_id):
        job = self.scenarios.iloc[scenario_id]
        self.active_fault = job
        # Ground truth: {category, reason, affected_node, affected_gpu}

    def _recover(self):
        self.active_fault = None
```

**Fault categories (from NSDI'24 paper Table 3):**

```
Infrastructure (~82% of failures):
  ├── NVLink Error        (30%)  → XID 43
  ├── CUDA Error          (16%)  → Various XID codes
  ├── Node Failure        (14%)  → Node unresponsive
  ├── ECC Error           (11%)  → XID 48, 63
  ├── Network Error        (5%)  → InfiniBand issues
  ├── Connection Error     (3%)  → NCCL connection
  ├── S3 Storage Error     (2%)  → Storage backend
  └── NCCL Timeout/Error   (1%)  → Collective communication

Framework (~14% of failures):
  ├── Dataloader Killed    (4%)  → Worker OOM during data loading
  ├── Out of Memory        (3%)  → GPU OOM
  ├── Runtime Error        (2%)  → PyTorch/TF runtime
  ├── Assertion Error       (2%)
  ├── Attribute Error       (1%)
  └── Value Error           (2%)

Script (~3% of failures):
  ├── File Not Found       (3%)
  └── Other errors        (<1%)  → TypeError, ImportError, etc.
```

**Work needed:**
- Fault injector class (follows existing FaultInjector pattern)
- Scenario preprocessor (from Task 1.3)
- Ground truth mapping to AIOpsLab evaluation format

**Estimated effort:** Low-Medium

---

### Phase 4: Problem Definitions & Evaluation

#### Task 4.1: GPU Task Types

Adapt the 3 supported task types for GPU cluster context:

**Detection:**
```python
# "Is there a GPU/infrastructure failure?"
# Agent checks GPU metrics, XID errors, job states
submit("Yes")  # or "No"
```

**Localization:**
```python
# "Which node/GPU caused the failure?"
submit(["10.140.1.10"])  # affected node IP(s)
# Or more specific:
submit(["10.140.1.10:gpu3"])  # node + specific GPU
```

**Analysis:**
```python
# "What type of failure?"
submit({
    "system_level": "Hardware",          # Hardware / Application / Framework
    "fault_type": "NVLink Error",        # From taxonomy above
    "category": "Infrastructure"         # Infrastructure / Framework / Script
})
```

**Work needed:**
- Define evaluation criteria for each task type
- Map ground truth from scenario definitions
- Decide localization granularity (node-level vs GPU-level)

**Estimated effort:** Medium

---

#### Task 4.2: Problem Definitions

Generate problem instances from the Kalos job trace.

**From Kalos dataset:**
- ~20K GPU jobs
- Filter: FAILED + NODE_FAIL + TIMEOUT states
- Estimated: ~8,000 failure scenarios (40% failure rate)
- Each generates 3 task types = ~24,000 problem instances

**From Seren dataset (future, if storage allows):**
- ~664K GPU jobs
- Much larger scale

**Work needed:**
- Problem class per fault category (or parameterized)
- Registry entries
- Ground truth from `sample_kalos_rca.py` output

**Estimated effort:** Medium

---

#### Task 4.3: GPU-Specific Evaluation

Extend evaluation for GPU cluster context.

```python
def eval(self, soln, trace, duration):
    # Detection: exact match on "Yes"/"No"
    # Localization: check if submitted node(s) match affected_node
    # Analysis: check category + reason match

    # GPU-specific metrics:
    self.add_result("affected_nodes", self.ground_truth['affected_node'])
    self.add_result("xid_code", self.ground_truth.get('xid_code'))
    self.add_result("job_gpu_count", self.ground_truth['gpu_num'])
```

**Work needed:**
- Evaluation logic for node-level localization
- Optional GPU-level localization scoring
- Category + reason matching for analysis task

**Estimated effort:** Low

---

### Phase 5: Agent Prompt & Instructions

#### Task 5.1: GPU Cluster Task Description & Instructions

The agent needs different instructions for GPU cluster debugging vs microservice debugging.

**Example task description for Detection:**
```
You are an AI operations assistant for a GPU training cluster.
The cluster runs distributed deep learning training jobs using A100 GPUs.

Your task: Determine if there is currently a GPU infrastructure failure
or job failure in the cluster.

Available APIs:
- get_gpu_metrics(namespace, node=None, duration=5): GPU utilization, temperature, memory, power
- get_node_metrics(namespace, node=None, duration=5): CPU, memory, InfiniBand traffic
- get_power_metrics(namespace, node=None, duration=5): IPMI power consumption
- get_job_info(job_id=None, state=None): Job metadata and status
- get_xid_errors(namespace, node=None, duration=5): GPU hardware error codes
- exec_shell(command): Execute shell commands
- submit(has_anomaly): Submit "Yes" or "No"

Cluster: acmetrace-kalos
GPU Type: NVIDIA A100 (8 per node)
```

**Work needed:**
- Task descriptions for Detection, Localization, Analysis
- Available actions list with GPU-specific APIs
- Cluster context information

**Estimated effort:** Low

---

## Implementation Order

```
Phase 1 (Data Layer)              ← Reuses OpenRCA preprocessing patterns
  1.1 Wide-to-Long Transformer
  1.2 GPU Metric Name Mapping
  1.3 Job Trace Preprocessor

Phase 2 (New APIs)                ← Extends OpenRCA action pattern
  2.1 GPU Metrics API
  2.2 Power Metrics API
  2.3 Job Info API
  2.4 XID Error API
  2.5 Node Metrics API

Phase 3 (Service Layer)           ← Follows OpenRCA Application pattern
  3.1 AcmeTraceService class
  3.2 GPU Stub Pods
  3.3 GPUFaultInjector

Phase 4 (Problems & Eval)         ← Follows OpenRCA Problem pattern
  4.1 GPU Task Types
  4.2 Problem Definitions
  4.3 GPU-Specific Evaluation

Phase 5 (Agent Interface)         ← New prompts
  5.1 Task Description & Instructions
```

**Estimated total: 15 tasks, builds on OpenRCA patterns**

---

## Resource Requirements

### Recommended: Start with Kalos cluster

| Component | Size |
|-----------|------|
| Kalos utilization data | ~12 GB (already downloaded) |
| Kalos job traces | ~9 MB |
| Preprocessed 90-min window | ~200 MB |
| Prometheus storage (backfilled) | ~50 MB |
| Stub pods (RAM) | ~200 MB |
| **Total new storage** | **~1 GB per scenario** |
| **Total RAM** | **~3 GB K8s overhead** |

### Future: Seren cluster

| Component | Size |
|-----------|------|
| Seren utilization data | ~67 GB |
| Seren job traces | ~99 MB |
| **Storage needed** | ~70 GB additional |

---

## Comparison: What Carries Over from OpenRCA

| Component | Reusable from OpenRCA? | Adaptation needed |
|-----------|----------------------|-------------------|
| Prometheus remote_write backfill | Yes, identical pattern | Different metric names |
| Timestamp anchoring (time_offset) | Yes, identical | Same logic |
| Application base class pattern | Yes, same inheritance | GPU-specific deploy |
| FaultInjector pattern | Yes, same pattern | GPU fault taxonomy |
| Problem/Task pattern | Yes, same pattern | GPU task definitions |
| Docker image (openrca-replayer) | Partially | Add GPU preprocessing scripts |
| Evaluation framework | Yes, same framework | GPU-specific metrics |
| Jaeger integration | No (no traces) | Not needed |
| Log replay containers | No (no logs) | Synthesized event logs instead |
| exec_shell / submit | Yes, identical | Same |

---

## Open Design Questions

1. **Localization granularity**: Node-level ("10.140.1.10") or GPU-level ("10.140.1.10:gpu3")?
   - Node-level is simpler but less precise
   - GPU-level requires XID error correlation (only XID 43 is prevalent in dataset)

2. **Job-to-node mapping**: AcmeTrace doesn't directly map jobs to specific nodes.
   - XID errors provide temporal correlation
   - Need heuristic: "XID error on node X during job Y's runtime → node X affected job Y"

3. **Multi-job scenarios**: Multiple jobs may fail simultaneously.
   - Single-job scenarios (simpler): one failed job per scenario
   - Multi-job scenarios (realistic): cluster-wide failure affecting multiple jobs

4. **Synthetic logs**: Should we generate event logs from job state transitions?
   - Pro: Gives agents something for get_logs() to return
   - Con: Fabricated data, not authentic
   - Recommendation: Generate minimal event logs (job started/failed/completed)

5. **Cluster topology**: Should agents know the network topology?
   - AcmeTrace uses InfiniBand (fat-tree topology)
   - Topology awareness helps diagnose network-related failures
   - Could expose via exec_shell("kubectl describe node ...") with enriched annotations
