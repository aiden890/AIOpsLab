# Alibaba Cluster Trace GPU 2020 Documentation

This document describes the Alibaba Cluster Trace GPU 2020 dataset, which contains GPU cluster traces from Alibaba's Platform for AI (PAI).

---

## Overview

The dataset contains **7 CSV files** with information about GPU cluster workloads, including jobs, tasks, instances, machine metrics, and sensor data.

### Dataset Summary

| File | Size | Description |
|------|------|-------------|
| `pai_job_table.csv` | 133.29 MB | Job-level information |
| `pai_task_table.csv` | 113.4 MB | Task-level information |
| `pai_instance_table.csv` | 1.98 GB | Instance-level information |
| `pai_sensor_table.csv` | 1.06 GB | Instance sensor metrics |
| `pai_machine_spec.csv` | 73.35 KB | Machine specifications |
| `pai_machine_metric.csv` | 434.33 MB | Machine-level metrics |
| `pai_group_tag_table.csv` | 115.52 MB | Instance grouping and workload tags |

### What are Job, Task, and Instance?

In distributed machine learning systems, workloads are organized in a hierarchical structure:

#### Job

A **Job** is simply **one submission by a user** - it can be small or large.

| Aspect | Description |
|--------|-------------|
| **What it is** | A single submission to the cluster |
| **Submitted by** | User via platform interface |
| **Contains** | One or more Tasks |
| **Duration** | Can be minutes, hours, or days |

**Important:** A job is NOT necessarily "complete training". It's just one submission.

```
┌─────────────────────────────────────────────────────────────────┐
│                    JOB SIZE EXAMPLES                             │
│                                                                  │
│  Small Job (minutes):          Large Job (hours/days):          │
│  - Run inference on 100 images - Train ResNet for 50 epochs     │
│  - Fine-tune for 1 epoch       - Process large dataset          │
│  - Test model checkpoint       - Full training run              │
└─────────────────────────────────────────────────────────────────┘
```

**Q: During LLM pretraining, is there only 1 job?**

**A: Usually NO!** Large training is typically broken into **multiple jobs**:

```
┌─────────────────────────────────────────────────────────────────┐
│              LLM PRETRAINING (e.g., GPT-like model)             │
│                                                                  │
│   Job 1          Job 2          Job 3          Job 4            │
│   ┌─────┐        ┌─────┐        ┌─────┐        ┌─────┐         │
│   │Step │   →    │Step │   →    │Step │   →    │Step │         │
│   │0-10K│        │10K- │        │20K- │        │30K- │         │
│   │     │        │20K  │        │30K  │        │40K  │         │
│   └──┬──┘        └──┬──┘        └──┬──┘        └──┬──┘         │
│      │              │              │              │              │
│      ▼              ▼              ▼              ▼              │
│   checkpoint     checkpoint     checkpoint     checkpoint       │
│   saved          saved          saved          saved            │
└─────────────────────────────────────────────────────────────────┘
```

**Why break into multiple jobs?**

| Reason | Explanation |
|--------|-------------|
| **Fault tolerance** | If job fails, restart from last checkpoint |
| **Resource limits** | Cluster may have max job duration (e.g., 24 hours) |
| **Scheduling fairness** | Can't hold GPUs forever, others need them |
| **Experimentation** | Try different hyperparameters between jobs |
| **Cost management** | Monitor and adjust resources between jobs |

**Real-world example:**

```
Training GPT-3 (hypothetical):
├── Job 1: Warmup phase (steps 0-1000)
├── Job 2: Phase 1 training (steps 1000-50000)
├── Job 3: Phase 1 continued (steps 50000-100000)  ← Job 2 hit time limit
├── Job 4: Learning rate decay phase (steps 100000-150000)
├── Job 5: Fine-tuning phase
└── Job 6: Final evaluation
```

So in the Alibaba trace, you'll see many jobs from the same user that are actually **parts of one large training run**.

#### Task

A **Task** is a specific role or component within a Job.

| Aspect | Description |
|--------|-------------|
| **What it is** | A distinct role in distributed training |
| **Part of** | A Job |
| **Contains** | One or more Instances |
| **Examples** | `worker`, `ps` (parameter server), `evaluator`, `chief` |

**Why multiple tasks?** Distributed ML often uses different roles:

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

#### Instance

An **Instance** is an actual running process/container.

| Aspect | Description |
|--------|-------------|
| **What it is** | A single running process on a machine |
| **Part of** | A Task |
| **Runs on** | A physical/virtual machine |
| **Examples** | `worker-0`, `worker-1`, `ps-0` |

**Why multiple instances?** For parallelism and fault tolerance:

```
┌─────────────────────────────────────────────────────────────────┐
│                    TASK: "worker"                                │
│                    (4 instances for data parallelism)            │
│                                                                  │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│   │ INSTANCE │  │ INSTANCE │  │ INSTANCE │  │ INSTANCE │       │
│   │ worker-0 │  │ worker-1 │  │ worker-2 │  │ worker-3 │       │
│   │          │  │          │  │          │  │          │       │
│   │ Machine  │  │ Machine  │  │ Machine  │  │ Machine  │       │
│   │   A      │  │   B      │  │   C      │  │   D      │       │
│   │ GPU: T4  │  │ GPU: T4  │  │ GPU: V100│  │ GPU: V100│       │
│   └──────────┘  └──────────┘  └──────────┘  └──────────┘       │
└─────────────────────────────────────────────────────────────────┘
```

#### Complete Example

```
┌─────────────────────────────────────────────────────────────────┐
│  USER: "Train BERT model with 8 GPUs"                           │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  JOB: bert_training_job_001                                      │
│  - user: alice                                                   │
│  - status: Terminated (success)                                  │
│  - start_time: 1000000                                          │
│  - end_time: 1003600 (1 hour later)                             │
└───────────────────────────┬─────────────────────────────────────┘
                            │
            ┌───────────────┴───────────────┐
            ▼                               ▼
┌───────────────────────┐       ┌───────────────────────┐
│  TASK: worker         │       │  TASK: ps             │
│  - inst_num: 8        │       │  - inst_num: 2        │
│  - plan_gpu: 100.0    │       │  - plan_gpu: 0        │
│  - plan_cpu: 800.0    │       │  - plan_cpu: 400.0    │
│  - gpu_type: V100     │       │  - gpu_type: null     │
└───────────┬───────────┘       └───────────┬───────────┘
            │                               │
    ┌───┬───┼───┬───┐               ┌───────┴───────┐
    ▼   ▼   ▼   ▼   ▼               ▼               ▼
  ┌───┐┌───┐┌───┐┌───┐           ┌───┐           ┌───┐
  │w-0││w-1││...││w-7│           │ps-0│           │ps-1│
  └─┬─┘└─┬─┘└───┘└─┬─┘           └─┬─┘           └─┬─┘
    │    │         │               │               │
    ▼    ▼         ▼               ▼               ▼
 Machine Machine Machine        Machine         Machine
   A       B       H              X               Y
```

#### Summary Table

| Level | What | How Many | Resource Allocation | Monitoring |
|-------|------|----------|---------------------|------------|
| **Job** | User's complete request | 1 per submission | - | Job status |
| **Task** | Role in distributed training | 1+ per job | CPU, GPU, Memory requests | Task status |
| **Instance** | Running process | 1+ per task | Actual resource usage | Sensor metrics |

### Data Hierarchy

```
┌─────────────────────────────────────────────────────────────────┐
│                           JOB                                    │
│                    (pai_job_table.csv)                          │
│                    - job_name, inst_id, user                     │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 1 job has 1+ tasks
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                          TASK                                    │
│                    (pai_task_table.csv)                         │
│                    - task_name, plan_cpu, plan_gpu               │
└───────────────────────────┬─────────────────────────────────────┘
                            │ 1 task has 1+ instances
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                        INSTANCE                                  │
│                   (pai_instance_table.csv)                       │
│                   - inst_name, worker_name, machine              │
└───────────────────────────┬─────────────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
     ┌────────────┐  ┌────────────┐  ┌────────────┐
     │  Sensor    │  │  Machine   │  │  Group     │
     │  Metrics   │  │  Metrics   │  │  Tags      │
     └────────────┘  └────────────┘  └────────────┘
```

---

## File Descriptions

### 1. pai_job_table.csv (133.29 MB)

Contains job-level information submitted by users.

| Column | Description |
|--------|-------------|
| `job_name` | Name of user's submitted job (desensitized) |
| `inst_id` | Instance ID assigned, joins with `pai_sensor_table` and `pai_group_tag_table` |
| `user` | User name (desensitized) |
| `status` | Job status: `Running`, `Terminated`, `Failed`, `Waiting` |
| `start_time` | Timestamp of job submission time |
| `end_time` | Timestamp of job completion time |

**Status Values:**

| Status | Meaning |
|--------|---------|
| `Terminated` | Successful completion |
| `Failed` | Job failed |
| `Running` | Currently executing |
| `Waiting` | Waiting to be scheduled |

**Note on Time:** Both `start_time` and `end_time` are in seconds and have been deducted by a constant for desensitization. When translated to Unix time in UTC+8 timezone ("Asia/Shanghai"), they preserve the same time of day and day of week as original traces, but have fake dates, months, and years.

---

### 2. pai_task_table.csv (113.4 MB)

Contains task-level information. Most jobs have one task, but some may have multiple tasks with different roles.

| Column | Description |
|--------|-------------|
| `job_name` | Job name, joins with `pai_job_table` |
| `task_name` | Task name/role (e.g., `ps`, `worker`, `evaluator`) |
| `inst_num` | Number of instances launched by the task |
| `status` | Task status |
| `start_time` | Timestamp of task launch time |
| `end_time` | Timestamp of task completion time |
| `plan_cpu` | CPU cores requested in percentage (600.0 = 6 vCPU cores) |
| `plan_mem` | Main memory requested (GB) |
| `plan_gpu` | GPUs requested in percentage (50.0 = 50% GPU) |
| `gpu_type` | Type of GPUs assigned to this task |

**GPU Types:**

| Type | Description |
|------|-------------|
| `T4` | NVIDIA Tesla T4 |
| `P100` | NVIDIA Tesla P100 |
| `V100` | NVIDIA Tesla V100 |
| `MISC` | Miscellaneous - older GPUs (K40m, K80, M60) |

**Scheduling Latency:** The gap between `job.start_time` and the earliest `task.start_time` indicates wait time before launching.

---

### 3. pai_instance_table.csv (1.98 GB)

Contains instance-level information for each task.

| Column | Description |
|--------|-------------|
| `job_name` | Job name, joins with `pai_job_table` |
| `task_name` | Task name, joins with `pai_task_table` |
| `inst_name` | Name of instance in each task |
| `worker_name` | Detailed instance identifier, joins with `pai_sensor_table` and `pai_machine_metric` |
| `status` | Instance status |
| `start_time` | Timestamp of instance launch time |
| `end_time` | Timestamp of instance completion time |
| `machine` | Machine name, joins with `pai_machine_spec` and `pai_machine_metric` |

---

### 4. pai_sensor_table.csv (1.06 GB)

Contains instance-level sensor metrics (CPU, GPU, Memory, I/O).

| Column | Description |
|--------|-------------|
| `job_name` | Job name, joins with `pai_job_table` |
| `task_name` | Task name, joins with `pai_task_table` |
| `worker_name` | Worker name, joins with `pai_instance_table` |
| `inst_id` | Instance ID, joins with `pai_job_table` |
| `machine` | Machine name, joins with `pai_instance_table` |
| `gpu_name` | Name of the GPU on that machine (not gpu_type) |
| `cpu_usage` | CPU cores used in percentage (600.0 = 6 vCPU cores) |
| `gpu_wrk_util` | GPU utilization in percentage (50.0 = 50% GPU) |
| `avg_mem` | Average main memory used (GB) |
| `max_mem` | Maximum main memory used (GB) |
| `avg_gpu_wrk_mem` | Average GPU memory used (GB) |
| `max_gpu_wrk_mem` | Maximum GPU memory used (GB) |
| `read` | Bytes of network input |
| `write` | Bytes of network output |
| `read_count` | Number of network read operations |
| `write_count` | Number of network write operations |

**Note:** All sensor metrics are collected per instance (indexed by `worker_name`), taking the average during the instance's lifetime (except `max_mem` and `max_gpu_wrk_mem` which are maximum values).

---

### 5. pai_machine_spec.csv (73.35 KB)

Contains machine hardware specifications.

| Column | Description |
|--------|-------------|
| `machine` | Machine name, joins with `pai_instance_table` |
| `gpu_type` | GPU type, same as `pai_task_table` |
| `cap_cpu` | CPU capacity (number of CPU cores) |
| `cap_mem` | Memory capacity (GB of main memory) |
| `cap_gpu` | GPU capacity (number of GPUs) |

---

### 6. pai_machine_metric.csv (434.33 MB)

Contains machine-level metrics during instance lifetime.

| Column | Description |
|--------|-------------|
| `worker_name` | Worker name, joins with `pai_instance_table` |
| `machine` | Machine name, joins with `pai_instance_table` |
| `start_time` | Timestamp of instance launch time |
| `end_time` | Timestamp of instance completion time |
| `machine_cpu_iowait` | CPU I/O wait (machine-level) |
| `machine_cpu_kernel` | CPU kernel usage (machine-level) |
| `machine_cpu_usr` | CPU user usage (machine-level) |
| `machine_gpu` | GPU utilization (machine-level) |
| `machine_load_1` | 1-minute load average (machine-level) |
| `machine_net_receive` | Network received bytes (machine-level) |
| `machine_num_worker` | Number of co-located instances/workers |
| `machine_cpu` | CPU overall usage (machine-level) |

**Note:** Machine-level metrics are averaged over the sensor data during the instance's (indexed by `worker_name`) lifetime.

---

### 7. pai_group_tag_table.csv (115.52 MB)

Contains instance grouping and workload classification tags.

| Column | Description |
|--------|-------------|
| `inst_id` | Instance ID, joins with `pai_job_table` |
| `user` | User name, joins with `pai_job_table` |
| `gpu_type_spec` | Empty if instance doesn't specify GPU type, else one of `gpu_type` values |
| `group` | Semantic tag for instances with similar customized inputs |
| `workload` | Deep Learning workload type (e.g., `graphlearn`, `ctr`, `bert`) |

**Group Tag:** Instances with the same `group` tag have similar customized inputs (entry scripts, command-line parameters, data sources/sinks) and are considered repeated instances.

**Workload Tag:** Around 9% of instances have this tag, indicating identified Deep Learning tasks.

---

## Join Keys (Foreign Keys)

```
┌────────────────────────────────────────────────────────────────────────────┐
│                           JOIN RELATIONSHIPS                                │
│                                                                            │
│  pai_job_table                                                             │
│       │                                                                    │
│       ├── job_name ──────────────▶ pai_task_table.job_name                │
│       │                           pai_instance_table.job_name              │
│       │                           pai_sensor_table.job_name                │
│       │                                                                    │
│       └── inst_id ───────────────▶ pai_sensor_table.inst_id               │
│                                   pai_group_tag_table.inst_id              │
│                                                                            │
│  pai_task_table                                                            │
│       │                                                                    │
│       └── task_name ─────────────▶ pai_instance_table.task_name           │
│                                   pai_sensor_table.task_name               │
│                                                                            │
│  pai_instance_table                                                        │
│       │                                                                    │
│       ├── worker_name ───────────▶ pai_sensor_table.worker_name           │
│       │                           pai_machine_metric.worker_name           │
│       │                                                                    │
│       └── machine ───────────────▶ pai_machine_spec.machine               │
│                                   pai_machine_metric.machine               │
│                                   pai_sensor_table.machine                 │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Example Queries

### Get Job with Task and Instance Details

```sql
SELECT
    j.job_name,
    j.user,
    j.status as job_status,
    t.task_name,
    t.plan_cpu,
    t.plan_gpu,
    t.gpu_type,
    i.inst_name,
    i.machine
FROM pai_job_table j
JOIN pai_task_table t ON j.job_name = t.job_name
JOIN pai_instance_table i ON t.job_name = i.job_name AND t.task_name = i.task_name
WHERE j.status = 'Terminated'
LIMIT 100;
```

### Get Instance Resource Usage

```sql
SELECT
    s.worker_name,
    s.cpu_usage,
    s.gpu_wrk_util,
    s.avg_mem,
    s.max_mem,
    s.avg_gpu_wrk_mem,
    m.cap_cpu,
    m.cap_mem,
    m.cap_gpu
FROM pai_sensor_table s
JOIN pai_instance_table i ON s.worker_name = i.worker_name
JOIN pai_machine_spec m ON i.machine = m.machine
LIMIT 100;
```

### Get Deep Learning Workload Distribution

```sql
SELECT
    workload,
    COUNT(*) as count
FROM pai_group_tag_table
WHERE workload IS NOT NULL AND workload != ''
GROUP BY workload
ORDER BY count DESC;
```

---

## Resource Planning vs Actual Usage

The dataset allows comparison between requested resources and actual usage:

| Planned (pai_task_table) | Actual (pai_sensor_table) |
|--------------------------|---------------------------|
| `plan_cpu` | `cpu_usage` |
| `plan_mem` | `avg_mem`, `max_mem` |
| `plan_gpu` | `gpu_wrk_util` |
| - | `avg_gpu_wrk_mem`, `max_gpu_wrk_mem` |

**Note:** Values are in percentage format where 100.0 = 1 core/GPU.

---

## Deep Learning Workload Types

Identified workloads in `pai_group_tag_table.workload` (approximately 9% of instances):

| Workload | Description |
|----------|-------------|
| `graphlearn` | Graph neural network learning |
| `ctr` | Click-through rate prediction |
| `bert` | BERT language model training |
| `cv` | Computer vision tasks |
| `nlp` | Natural language processing |
| `recommendation` | Recommendation systems |

---

## Data Statistics

| Metric | Value |
|--------|-------|
| Total Data Size | ~3.8 GB |
| Time Period | Desensitized (preserves day-of-week, time-of-day) |
| Timezone | UTC+8 (Asia/Shanghai) |
| GPU Types | T4, P100, V100, MISC |

---

## References

- [Alibaba Cluster Trace Program](https://github.com/alibaba/clusterdata)
- Trace Analysis Paper (referenced in dataset documentation)
