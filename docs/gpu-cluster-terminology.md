# GPU Cluster Terminology: Job, Task, Instance, Node, Machine, GPU

This document explains the hierarchy and relationships between key concepts in GPU cluster systems.

---

## Quick Reference

| Term | Level | Definition |
|------|-------|------------|
| **Job** | Workload | One user submission to the cluster |
| **Task** | Role | A specific role within a job (worker, ps, evaluator) |
| **Instance** | Process | A running container/process for a task |
| **Node/Machine** | Hardware | A physical server (same thing, different names) |
| **GPU** | Device | A single GPU card on a node |

---

## Hierarchy Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              JOB                                             │
│                    "Train LLaMA with 64 GPUs"                               │
│                         job_id: dlctk696s0jbvitv                            │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
┌─────────────────────────────┐     ┌─────────────────────────────┐
│          TASK               │     │          TASK               │
│        "worker"             │     │       "evaluator"           │
│      (trains model)         │     │    (validates model)        │
└─────────────┬───────────────┘     └─────────────┬───────────────┘
              │                                   │
    ┌────┬────┼────┬────┐                        │
    ▼    ▼    ▼    ▼    ▼                        ▼
┌──────┐┌──────┐┌──────┐┌──────┐           ┌──────┐
│Inst 0││Inst 1││Inst 2││Inst 3│           │Inst 0│
│      ││      ││      ││      │           │      │
└──┬───┘└──┬───┘└──┬───┘└──┬───┘           └──┬───┘
   │       │       │       │                  │
   ▼       ▼       ▼       ▼                  ▼
┌──────┐┌──────┐┌──────┐┌──────┐           ┌──────┐
│Node 0││Node 1││Node 2││Node 3│           │Node 4│
│8 GPUs││8 GPUs││8 GPUs││8 GPUs│           │8 GPUs│
└──────┘└──────┘└──────┘└──────┘           └──────┘
```

---

## 1. Job

A **Job** is a single submission by a user to the cluster.

| Aspect | Description |
|--------|-------------|
| **What it is** | One complete workload submission |
| **Submitted by** | User via scheduler (Slurm, Kubernetes, etc.) |
| **Contains** | One or more Tasks |
| **Duration** | Minutes to days |
| **Resources** | Requests nodes, GPUs, CPUs, memory |

### Example

```
Job: dlctk696s0jbvitv
├── user: uf794
├── node_num: 8
├── gpu_num: 64
├── state: FAILED
└── duration: 18 seconds
```

### Important Note

A job is NOT necessarily "complete training". Large training runs are often split into multiple jobs:

```
Training GPT-3 (hypothetical):
├── Job 1: Warmup phase (steps 0-1000)
├── Job 2: Phase 1 training (steps 1000-50000)
├── Job 3: Phase 1 continued (steps 50000-100000)  ← Job 2 hit time limit
├── Job 4: Learning rate decay phase
└── Job 5: Final evaluation
```

---

## 2. Task

A **Task** is a specific role or component within a Job.

| Aspect | Description |
|--------|-------------|
| **What it is** | A distinct role in distributed training |
| **Part of** | A Job |
| **Contains** | One or more Instances |
| **Examples** | `worker`, `ps` (parameter server), `evaluator`, `chief` |

### Common Task Types

| Task | Purpose | GPU Usage |
|------|---------|-----------|
| `worker` | Computes gradients, trains model | High |
| `ps` (parameter server) | Stores and updates weights | Low/None |
| `evaluator` | Validates model periodically | Medium |
| `chief` | Coordinates training | Low |

### Example: Multi-Task Job

```
┌─────────────────────────────────────────────────────────────────┐
│                         JOB                                      │
│                 "Train ResNet Model"                             │
│                                                                  │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│   │    TASK     │  │    TASK     │  │    TASK     │             │
│   │   "worker"  │  │    "ps"     │  │ "evaluator" │             │
│   │             │  │ (parameter  │  │             │             │
│   │ Computes    │  │   server)   │  │ Validates   │             │
│   │ gradients   │  │ Stores      │  │ model       │             │
│   │             │  │ weights     │  │             │             │
│   └─────────────┘  └─────────────┘  └─────────────┘             │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Instance

An **Instance** is an actual running process or container.

| Aspect | Description |
|--------|-------------|
| **What it is** | A single running process on a machine |
| **Part of** | A Task |
| **Runs on** | A physical/virtual machine (Node) |
| **Examples** | `worker-0`, `worker-1`, `ps-0` |

### Why Multiple Instances?

For parallelism and fault tolerance:

```
┌─────────────────────────────────────────────────────────────────┐
│                    TASK: "worker"                                │
│                    (4 instances for data parallelism)            │
│                                                                  │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│   │ INSTANCE │  │ INSTANCE │  │ INSTANCE │  │ INSTANCE │        │
│   │ worker-0 │  │ worker-1 │  │ worker-2 │  │ worker-3 │        │
│   │          │  │          │  │          │  │          │        │
│   │ Node A   │  │ Node B   │  │ Node C   │  │ Node D   │        │
│   │ 8 GPUs   │  │ 8 GPUs   │  │ 8 GPUs   │  │ 8 GPUs   │        │
│   └──────────┘  └──────────┘  └──────────┘  └──────────┘        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. Node vs Machine

**They are the SAME thing!** Different terminology used in different contexts.

| Term | Context | Example Usage |
|------|---------|---------------|
| **Node** | Cluster/HPC terminology | "8-node job" |
| **Machine** | General/Cloud terminology | "Deploy to machine" |
| **Server** | Hardware terminology | "Physical server" |
| **Host** | Networking terminology | "Host IP: 172.31.15.112" |

### What is a Node/Machine?

A physical (or virtual) server with:

```
┌─────────────────────────────────────────────────────────────────┐
│  NODE / MACHINE: 172.31.15.112                                  │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ Hardware Specifications                                      ││
│  │ ├── CPU: 128 cores (AMD EPYC / Intel Xeon)                  ││
│  │ ├── Memory: 1TB - 2TB RAM                                   ││
│  │ ├── Network: InfiniBand / RoCE (100-400 Gbps)              ││
│  │ ├── Storage: NVMe SSDs                                      ││
│  │ └── GPUs: 8 × NVIDIA A100 (80GB each)                       ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                  │
│  ┌────────┬────────┬────────┬────────┬────────┬────────┬───────┐│
│  │ GPU-0  │ GPU-1  │ GPU-2  │ GPU-3  │ GPU-4  │ GPU-5  │ GPU-6 ││
│  │ 80GB   │ 80GB   │ 80GB   │ 80GB   │ 80GB   │ 80GB   │ 80GB  ││
│  └────────┴────────┴────────┴────────┴────────┴────────┴───────┘│
│  ┌────────┐                                                      │
│  │ GPU-7  │                                                      │
│  │ 80GB   │                                                      │
│  └────────┘                                                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 5. GPU

A **GPU** is a single graphics processing unit on a node.

| Aspect | Description |
|--------|-------------|
| **What it is** | Single GPU device (e.g., NVIDIA A100) |
| **Located on** | A Node/Machine |
| **Identified by** | IP + GPU index (e.g., `172.31.15.112-6`) |
| **Memory** | 40GB - 80GB HBM |

### GPU Indexing

On an 8-GPU node:

```
Node: 172.31.15.112
├── GPU 0: 172.31.15.112-0
├── GPU 1: 172.31.15.112-1
├── GPU 2: 172.31.15.112-2
├── GPU 3: 172.31.15.112-3
├── GPU 4: 172.31.15.112-4
├── GPU 5: 172.31.15.112-5
├── GPU 6: 172.31.15.112-6
└── GPU 7: 172.31.15.112-7
```

---

## 6. Complete Example: 64-GPU Training Job

```
┌─────────────────────────────────────────────────────────────────┐
│  JOB: "Train GPT model"                                         │
│  ├── job_id: dlctk696s0jbvitv                                   │
│  ├── user: uf794                                                │
│  ├── node_num: 8                                                │
│  ├── gpu_num: 64                                                │
│  └── state: FAILED                                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  TASK: "worker" (single task in this job)                       │
│  └── instances: 8 (one per node)                                │
└─────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────┬─────────┬─┴───────┬─────────┐
        ▼         ▼         ▼         ▼         ▼
┌─────────────────────────────────────────────────────────────────┐
│  INSTANCES (8 total, one per node)                              │
│                                                                  │
│  Instance 0    Instance 1    Instance 2    ...    Instance 7    │
│  (Node 0)      (Node 1)      (Node 2)             (Node 7)      │
└─────────────────────────────────────────────────────────────────┘
                              │
        ┌─────────┬─────────┬─┴───────┬─────────┐
        ▼         ▼         ▼         ▼         ▼
┌─────────────────────────────────────────────────────────────────┐
│  NODES / MACHINES (8 physical servers)                          │
│                                                                  │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐               │
│  │172.31.15.112│ │172.31.15.118│ │172.31.0.234 │ ...           │
│  │   Node 0    │ │   Node 1    │ │   Node 2    │               │
│  │   8 GPUs    │ │   8 GPUs    │ │   8 GPUs    │               │
│  └─────────────┘ └─────────────┘ └─────────────┘               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  GPUs (64 total = 8 nodes × 8 GPUs)                             │
│                                                                  │
│  Node 0: 172.31.15.112                                          │
│  ├── 172.31.15.112-0  ──┐                                       │
│  ├── 172.31.15.112-1    │                                       │
│  ├── 172.31.15.112-2    │                                       │
│  ├── 172.31.15.112-3    ├── 8 GPUs                              │
│  ├── 172.31.15.112-4    │                                       │
│  ├── 172.31.15.112-5    │                                       │
│  ├── 172.31.15.112-6    │                                       │
│  └── 172.31.15.112-7  ──┘                                       │
│                                                                  │
│  Node 1: 172.31.15.118                                          │
│  ├── 172.31.15.118-0                                            │
│  └── ... (8 GPUs)                                               │
│                                                                  │
│  ... (6 more nodes)                                             │
│                                                                  │
│  Total: 8 nodes × 8 GPUs = 64 GPUs                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 7. Mapping to Datasets

### AcmeTrace (Kalos/Seren)

| Concept | In Job Trace | In Utilization Files |
|---------|--------------|---------------------|
| **Job** | `job_id` column | - |
| **Task** | Not tracked (single-task) | - |
| **Instance** | Implied by `gpu_num` | - |
| **Node/Machine** | `node_num` column | `172.31.15.112` (IP) |
| **GPU** | `gpu_num` column | `172.31.15.112-6` (IP-GPU_ID) |

### Alibaba GPU 2020

| Concept | Table | Column |
|---------|-------|--------|
| **Job** | `pai_job_table.csv` | `job_name` |
| **Task** | `pai_task_table.csv` | `task_name` (worker, ps, etc.) |
| **Instance** | `pai_instance_table.csv` | `inst_name`, `worker_name` |
| **Machine** | `pai_machine_spec.csv` | `machine` |
| **GPU** | `pai_task_table.csv` | `gpu_type`, `plan_gpu` |

---

## 8. Failure Impact by Level

| Failure Level | Scope | Impact | Recovery |
|---------------|-------|--------|----------|
| **GPU failure** | 1 GPU | 1/64 capacity lost | May continue or checkpoint |
| **Node failure** | 8 GPUs | 8/64 capacity lost | Job likely fails |
| **Instance failure** | 1 process | Training stops | Restart from checkpoint |
| **Task failure** | Multiple instances | Role unavailable | Job fails |
| **Job failure** | Entire workload | All resources released | Resubmit job |

### Example: XID 43 Error

```
GPU 172.31.15.112-6 → XID 43 (GPU fell off bus)
                    ↓
             GPU failure
                    ↓
        Training process crashes
                    ↓
          Instance 0 fails
                    ↓
           Task "worker" fails
                    ↓
             Job FAILED
                    ↓
        state: FAILED, fail_time: 2023-05-17 11:01:16
```

---

## 9. Summary Table

| Level | Quantity (64-GPU Job) | Identified By | Failure Impact |
|-------|----------------------|---------------|----------------|
| **Job** | 1 | `job_id` | Complete failure |
| **Task** | 1-3 | `task_name` | Role lost |
| **Instance** | 8 | `instance_id` | Process dies |
| **Node/Machine** | 8 | IP address | 8 GPUs lost |
| **GPU** | 64 | IP-GPU_ID | 1 GPU lost |

---

## 10. Key Takeaways

1. **Node = Machine** - Same thing, different terminology

2. **Hierarchy flows down**:
   ```
   Job → Task → Instance → Node → GPU
   ```

3. **Failures propagate up**:
   ```
   GPU fails → Instance fails → Task fails → Job fails
   ```

4. **Resource allocation**:
   - Jobs request `node_num` and `gpu_num`
   - Scheduler assigns specific nodes/GPUs
   - Instances run on assigned resources

5. **Monitoring granularity**:
   - Node-level: CPU, memory utilization
   - GPU-level: GPU util, temperature, memory, XID errors
