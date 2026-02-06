# AcmeTrace Kalos Cluster Dataset Documentation

This document describes the Kalos cluster dataset from AcmeTrace (Shanghai AI Lab, NSDI'24).

---

## Overview

| Feature | Value |
|---------|-------|
| **Cluster** | Kalos |
| **Period** | May - August 2023 |
| **Total Jobs** | 62,413 |
| **GPU Jobs** | ~20,000 |
| **CPU Jobs** | ~42,000 |
| **GPUs** | A100 |
| **Sampling Interval** | 15 seconds |

---

## File Locations

```
acme_cluster_dataset/AcmeTrace/
├── data/
│   ├── job_trace/
│   │   └── trace_kalos.csv          # Job-level trace (62,413 jobs)
│   └── utilization/
│       └── kalos/
│           ├── GPU_UTIL.csv         # GPU utilization (%)
│           ├── GPU_TEMP.csv         # GPU temperature
│           ├── FB_USED.csv          # GPU frame buffer used
│           ├── FB_FREE.csv          # GPU frame buffer free
│           ├── POWER_USAGE.csv      # GPU power consumption
│           ├── SM_ACTIVE.csv        # Streaming multiprocessor activity
│           ├── SM_OCCUPANCY.csv     # SM occupancy
│           ├── MEM_CLOCK.csv        # Memory clock
│           ├── MEM_COPY_UTIL.csv    # Memory copy utilization
│           ├── MEMORY_TEMP.csv      # Memory temperature
│           ├── PIPE_TENSOR_ACTIVE.csv # Tensor core activity
│           ├── DRAM_ACTIVE.csv      # DRAM activity
│           ├── NODE_CPU_UTILIZATION.csv    # Node CPU usage
│           ├── NODE_MEMORY_UTILIZATION.csv # Node memory usage
│           └── XID_ERRORS.csv       # GPU error codes (failure data)
```

---

## 1. Job Trace (`trace_kalos.csv`)

### Description

Contains job-level information for all 62,413 jobs submitted to the Kalos cluster scheduler.

### Schema

| Column | Type | Description |
|--------|------|-------------|
| `job_id` | string | Unique job identifier (e.g., `dlctk696s0jbvitv`) |
| `user` | string | Hashed user ID with prefix `uf` (e.g., `uf794`) |
| `node_num` | int | Number of nodes requested |
| `gpu_num` | int | Number of GPUs requested |
| `cpu_num` | int | Number of CPUs requested |
| `mem_per_pod_GB` | float | Memory per pod (GB) |
| `shared_mem_per_pod` | float | Shared memory per pod |
| `type` | string | Workload type (e.g., `Other`) |
| `state` | string | **Job termination status (failure label)** |
| `submit_time` | datetime | Job submission time (UTC+0) |
| `start_time` | datetime | Job start execution time |
| `end_time` | datetime | Job termination time |
| `fail_time` | datetime | **Time when failure occurred** (if failed) |
| `stop_time` | datetime | Time when job was stopped (if cancelled) |
| `duration` | int | Execution time in seconds (`end_time - start_time`) |
| `queue` | float | Queue wait time in seconds (`start_time - submit_time`) |
| `gpu_time` | float | Total GPU-seconds (`duration × gpu_num`) |

### Job States (Failure Labels)

| State | Count | Percentage | Description |
|-------|-------|------------|-------------|
| `COMPLETED` | 47,311 | 75.8% | Successfully finished |
| `FAILED` | 13,836 | 22.2% | Terminated due to errors |
| `CANCELLED` | 1,263 | 2.0% | Terminated by user |
| `RUNNING` | 3 | <0.1% | Still executing (snapshot artifact) |
| `TIMEOUT` | rare | - | Exceeded time limit |
| `NODE_FAIL` | rare | - | Hardware failure |

### Example Row

```csv
job_id,user,node_num,gpu_num,cpu_num,mem_per_pod_GB,shared_mem_per_pod,type,state,submit_time,start_time,end_time,fail_time,stop_time,duration,queue,gpu_time
dlctk696s0jbvitv,uf794,8,64,960,1000,100.0,Other,FAILED,2023-05-17 11:00:58+00:00,2023-05-17 11:01:08+00:00,2023-05-17 11:01:16+00:00,2023-05-17 11:01:16+00:00,,18,10.0,1152.0
```

---

## 2. Utilization Files (Time-Series Metrics)

### Common Format

All utilization files share the same structure:

| Column | Description |
|--------|-------------|
| `Time` | Timestamp at 15-second intervals (e.g., `2023-08-12 10:51:15+08:00`) |
| `{IP}-{GPU_ID}` | Metric value for each GPU (e.g., `172.31.15.112-6`) |

For node-level metrics (CPU/Memory), columns are just IP addresses without GPU IDs.

### Statistics

| File | Rows | Columns | Description |
|------|------|---------|-------------|
| `GPU_UTIL.csv` | 77,403 | 2,345 | GPU utilization (0-100%) |
| `GPU_TEMP.csv` | 77,472 | ~2,345 | GPU temperature (°C) |
| `FB_USED.csv` | 77,403 | ~2,345 | Frame buffer memory used (MB) |
| `FB_FREE.csv` | 77,403 | ~2,345 | Frame buffer memory free (MB) |
| `POWER_USAGE.csv` | 77,403 | ~2,345 | Power consumption (W) |
| `SM_ACTIVE.csv` | 86,742 | ~2,345 | Streaming multiprocessor activity (%) |
| `SM_OCCUPANCY.csv` | 86,742 | ~2,345 | SM occupancy (%) |
| `MEM_CLOCK.csv` | 77,403 | ~2,345 | Memory clock frequency (MHz) |
| `MEM_COPY_UTIL.csv` | 77,403 | ~2,345 | Memory copy utilization (%) |
| `MEMORY_TEMP.csv` | 77,403 | ~2,345 | HBM memory temperature (°C) |
| `PIPE_TENSOR_ACTIVE.csv` | 73,878 | ~2,345 | Tensor core pipeline activity (%) |
| `DRAM_ACTIVE.csv` | 1 | - | DRAM activity (minimal data) |
| `NODE_CPU_UTILIZATION.csv` | 108,757 | ~300 | Per-node CPU usage (%) |
| `NODE_MEMORY_UTILIZATION.csv` | 104,390 | ~300 | Per-node memory usage (%) |
| `XID_ERRORS.csv` | 78,843 | ~2,345 | **GPU error codes (failure data)** |

### Column Naming Convention

- **GPU metrics**: `{IP}-{GPU_INDEX}` where GPU_INDEX is 0-7 for 8-GPU nodes
  - Example: `172.31.15.112-6` = GPU #6 on node 172.31.15.112
- **Node metrics**: Just `{IP}`
  - Example: `172.31.0.64`

---

## 3. XID_ERRORS.csv (GPU Failure Data)

### Description

Contains NVIDIA XID error codes collected from DCGM (Data Center GPU Manager). This is the **GPU-level failure/error data** mentioned in the paper.

### Schema

| Column | Description |
|--------|-------------|
| `Time` | Timestamp (15-second intervals) |
| `{IP}-{GPU_ID}` | XID error code for each GPU |

### XID Error Codes Found

| XID Code | Meaning | Severity |
|----------|---------|----------|
| `0` or empty | No error | - |
| `31` | GPU memory page retirement/ECC error | High |
| `43` | GPU has fallen off the bus | Critical |

### Example

```csv
Time,172.31.15.112-6,172.31.15.118-2,...
2023-08-15 15:30:15+08:00,0.0,0.0,...
2023-08-15 15:30:30+08:00,0.0,43.0,...  # XID 43 error on GPU 172.31.15.118-2
```

### XID Error Reference

Common NVIDIA XID errors:

| XID | Description |
|-----|-------------|
| 13 | Graphics Engine Exception |
| 31 | GPU memory page retirement / ECC error |
| 43 | GPU has fallen off the bus (PCIe/NVLink failure) |
| 45 | Preemptive cleanup, due to previous errors |
| 48 | Double Bit ECC Error |
| 63 | ECC page retirement or row remapping recording event |
| 64 | ECC page retirement or row remapping recording failure |
| 74 | NVLink Error |
| 79 | GPU has fallen off the bus (alternate) |

---

## 4. Failure Data Summary

The dataset provides failure information at **two levels**:

### Job-Level Failures (`trace_kalos.csv`)

- **`state` column**: FAILED, CANCELLED, TIMEOUT, NODE_FAIL
- **`fail_time` column**: Exact timestamp when failure occurred
- **22.2% of jobs failed** (13,836 out of 62,413)

### GPU-Level Failures (`XID_ERRORS.csv`)

- **XID error codes** at 15-second granularity
- Primarily XID 43 (GPU off bus) and XID 31 (memory errors)
- Can correlate with job failures by matching timestamps

### Correlating Failures

To analyze root causes, you can:

1. Find failed jobs from `trace_kalos.csv`
2. Get the failure time window (`start_time` to `fail_time`)
3. Query `XID_ERRORS.csv` for that time window
4. Check GPU metrics (temperature, utilization) before failure

---

## 5. About Missing "Failure Logs"

The paper mentions "runtime logs" but the public dataset contains:

| What Paper Mentions | What's Available | Notes |
|---------------------|------------------|-------|
| "Runtime logs" | `XID_ERRORS.csv` | Structured GPU error codes, not raw text logs |
| "Hardware monitoring data" | All utilization CSVs | Available |
| "Job logs" | `trace_kalos.csv` | Job metadata with failure states |
| "stdout/stderr" | **Not included** | Not released publicly |

The raw text logs (stdout/stderr from training jobs) are **not publicly released** due to privacy concerns. The structured error codes and metrics are available instead.

---

## 6. Usage Examples

### Load Job Trace

```python
import pandas as pd

# Load job trace
jobs = pd.read_csv('acme_cluster_dataset/AcmeTrace/data/job_trace/trace_kalos.csv')

# Filter failed jobs
failed_jobs = jobs[jobs['state'] == 'FAILED']
print(f"Failed jobs: {len(failed_jobs)} ({len(failed_jobs)/len(jobs)*100:.1f}%)")

# Get failure time distribution
failed_jobs['fail_time'] = pd.to_datetime(failed_jobs['fail_time'])
```

### Load GPU Metrics

```python
import pandas as pd

# Load GPU utilization (large file, use chunks if needed)
gpu_util = pd.read_csv(
    'acme_cluster_dataset/AcmeTrace/data/utilization/kalos/GPU_UTIL.csv',
    parse_dates=['Time'],
    index_col='Time'
)

# Get specific GPU
gpu_id = '172.31.15.112-6'
if gpu_id in gpu_util.columns:
    print(gpu_util[gpu_id].describe())
```

### Analyze XID Errors

```python
import pandas as pd

# Load XID errors
xid = pd.read_csv(
    'acme_cluster_dataset/AcmeTrace/data/utilization/kalos/XID_ERRORS.csv',
    parse_dates=['Time'],
    index_col='Time'
)

# Find all XID 43 errors (GPU fell off bus)
xid_43 = (xid == 43).sum()
gpus_with_errors = xid_43[xid_43 > 0]
print(f"GPUs with XID 43 errors: {len(gpus_with_errors)}")
```

---

## 7. References

- **Paper**: [Characterization of Large Language Model Development in the Datacenter](https://arxiv.org/abs/2403.07648) (NSDI'24)
- **GitHub**: https://github.com/InternLM/AcmeTrace
- **HuggingFace**: https://huggingface.co/datasets/Qinghao/AcmeTrace
- **NVIDIA XID Errors**: https://docs.nvidia.com/deploy/xid-errors/
