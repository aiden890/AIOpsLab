# GPU Cluster Datasets for Root Cause Analysis (RCA)

This document summarizes publicly available GPU cluster datasets suitable for Root Cause Analysis with temporal metrics and failure labeling.

---

## Summary Comparison

| Dataset | GPU Data | Temporal | Failure Labels | Size | Best For |
|---------|----------|----------|----------------|------|----------|
| **AcmeTrace** | ✅ A100 | ✅ 15s intervals | ✅ Job status | ~80GB | GPU cluster RCA |
| **RCAEval** | ❌ | ✅ Time-series | ✅ 735 cases, 11 fault types | ~2GB | Microservice RCA |
| **OpenRCA** | ❌ | ✅ | ✅ 335 failures | 68GB | LLM-based RCA |
| **Alibaba GPU 2020** | ✅ | ❌ Averaged only | ❌ | ~4GB | Scheduling research |
| **Alibaba Microservices 2022** | ❌ | ✅ 60s intervals | ❌ | ~10GB | Performance analysis |

---

## Recommended: AcmeTrace (Shanghai AI Lab)

**The best dataset for GPU cluster RCA with temporal data and failure labels.**

### Overview

| Feature | Details |
|---------|---------|
| **Source** | Shanghai AI Laboratory |
| **Publication** | NSDI'24 |
| **Duration** | 6 months (March - August 2023) |
| **Scale** | 4,704 A100 GPUs across 2 clusters |
| **Total Jobs** | 880,740 jobs (470,497 GPU jobs) |
| **Size** | ~80GB (full), 109MB (job traces only) |

### Download Links

- **GitHub (Job Traces):** https://github.com/InternLM/AcmeTrace
- **HuggingFace (Full Dataset):** https://huggingface.co/datasets/Qinghao/AcmeTrace

### Data Contents

#### 1. Job Traces
- Job submission, start, and end timestamps
- Completion status with failure labels
- Requested resources (GPU, CPU, memory)
- Job metadata

#### 2. Resource Utilization (Time-Series)
Collected at **15-second intervals**:

| Metric Source | Metrics |
|---------------|---------|
| **NVIDIA DCGM** | GPU utilization, GPU memory, temperature, power |
| **IPMI** | Server power consumption |
| **Prometheus** | CPU utilization, DRAM usage, network I/O |

#### 3. Failure Labels

| Status | Description |
|--------|-------------|
| `COMPLETED` | Job finished successfully |
| `FAILED` | Job failed during execution |
| `CANCELLED` | Job was cancelled by user |
| `TIMEOUT` | Job exceeded time limit |
| `NODE_FAIL` | Hardware/infrastructure failure |

### Clusters

| Cluster | GPU Jobs | CPU Jobs | GPUs |
|---------|----------|----------|------|
| Seren | 664,000 | 368,000 | Large scale |
| Kalos | 20,000 | 42,000 | Smaller scale |

### Use Cases

- GPU failure prediction
- LLM training workload analysis
- Resource utilization optimization
- Infrastructure failure diagnosis
- Job scheduling optimization

### Citation

```bibtex
@inproceedings{hu2024characterization,
  title={Characterization of Large Language Model Development in the Datacenter},
  author={Hu, Qinghao and others},
  booktitle={NSDI},
  year={2024}
}
```

---

## Alternative: RCAEval

**Best for microservice RCA benchmarking (no GPU data).**

### Overview

| Feature | Details |
|---------|---------|
| **Source** | WWW'25, ASE'24 |
| **Failure Cases** | 735 total |
| **Fault Types** | 11 types |
| **Data Types** | Metrics, logs, traces |

### Download

- **GitHub:** https://github.com/phamquiluan/RCAEval

### Datasets

| Suite | Cases | Data Types | Fault Types |
|-------|-------|------------|-------------|
| RE1 | 375 | Metrics only | CPU, MEM, DISK, DELAY, LOSS |
| RE2 | 270 | Metrics, logs, traces | + SOCKET |
| RE3 | 90 | Metrics, logs, traces | Code-level (F1-F5) |

### Fault Types

| Fault | Description |
|-------|-------------|
| CPU | CPU resource exhaustion |
| MEM | Memory resource exhaustion |
| DISK | Disk I/O issues |
| DELAY | Network latency injection |
| LOSS | Network packet loss |
| SOCKET | Socket connection issues |

### Features

- Annotated root cause service labels
- Root cause indicator (specific metric/log)
- Fault injection timestamps
- 15 reproducible baseline methods

---

## Alternative: OpenRCA

**Best for evaluating LLM-based RCA approaches.**

### Overview

| Feature | Details |
|---------|---------|
| **Source** | ICLR 2025 |
| **Failure Cases** | 335 |
| **Systems** | 3 enterprise software systems |
| **Size** | 68GB telemetry data |

### Download

- **Paper:** https://openreview.net/forum?id=M4qNIzQYpd

### Data Types

- Logs
- Metrics
- Traces

### Notes

- Designed for evaluating LLM reasoning on RCA tasks
- Best model (Claude 3.5 with RCA-agent) solved only 11.34% of cases
- Requires long-context reasoning over heterogeneous data

---

## Not Recommended: Alibaba GPU 2020

**Why it's not suitable for temporal RCA:**

| Issue | Details |
|-------|---------|
| **No temporal data** | Metrics are averaged over instance lifetime |
| **No failure labels** | Only job status (Running, Terminated, Failed, Waiting) |
| **Single record per instance** | No time-series, just aggregated values |

### What it contains

- `pai_sensor_table.csv`: One row per instance with `avg_mem`, `avg_gpu_wrk_mem`
- `pai_machine_metric.csv`: Averaged machine-level metrics per instance

### Better suited for

- Cluster scheduling simulation
- Resource allocation research
- Workload characterization

---

## Other Datasets (Limited)

### Alibaba Microservices 2021/2022

| Feature | 2021 | 2022 |
|---------|------|------|
| Duration | 12 hours | 13 days |
| Temporal | ✅ 30-60s intervals | ✅ 60s intervals |
| GPU Data | ❌ | ❌ |
| Failure Labels | ❌ | ❌ |

**Good for:** Performance analysis, call graph analysis
**Not good for:** RCA (no failure labels)

### Meta/Microsoft GPU Clusters

- Research papers published but **no public datasets**
- Internal production data only

---

## Recommendations by Use Case

| Use Case | Recommended Dataset |
|----------|---------------------|
| GPU cluster failure prediction | **AcmeTrace** |
| GPU workload RCA | **AcmeTrace** |
| Microservice RCA | **RCAEval** |
| LLM-based RCA evaluation | **OpenRCA** |
| Multi-source RCA (metrics + logs + traces) | **RCAEval RE2/RE3** |

---

## References

- [AcmeTrace GitHub](https://github.com/InternLM/AcmeTrace)
- [AcmeTrace Paper (NSDI'24)](https://arxiv.org/abs/2403.07648)
- [RCAEval GitHub](https://github.com/phamquiluan/RCAEval)
- [RCAEval Paper](https://arxiv.org/abs/2412.17015)
- [OpenRCA (ICLR 2025)](https://openreview.net/forum?id=M4qNIzQYpd)
- [Alibaba ClusterData](https://github.com/alibaba/clusterdata)
- [DevOps Dataset Collection](https://github.com/mooselab/DevOpsDataCollection)
