# Distributed Training Architecture

This document explains distributed deep learning training architecture, task types, and parallelism in GPU clusters.

---

## Task Names in Alibaba Trace

| Task Name | Framework | Role | Uses GPU? |
|-----------|-----------|------|-----------|
| `xtensorflow` | TensorFlow | Worker (compute gradients) | Yes |
| `PyTorchWorker` | PyTorch | Worker (compute gradients) | Yes |
| `xComputeWorker` | Generic/PAI | General compute worker | Yes |
| `ps` | Any | Parameter Server (store weights) | No |
| `evaluator` | Any | Evaluate model periodically | Sometimes |
| `chief` | TensorFlow | Coordinator worker | Yes |

---

## What Each Task Does

### Workers (`xtensorflow`, `PyTorchWorker`, `xComputeWorker`)

**Role:** Compute gradients using GPUs

```
┌─────────────────────────────────────────────────────────────────┐
│                         WORKER TASK                              │
│                                                                  │
│   1. Receive model weights from PS (or other workers)           │
│   2. Load batch of training data                                │
│   3. Forward pass: predictions = model(data)     ← GPU          │
│   4. Backward pass: gradients = loss.backward()  ← GPU          │
│   5. Send gradients to PS (or aggregate with others)            │
│   6. Repeat                                                      │
│                                                                  │
│   Resources: High GPU, Medium CPU, High Memory                  │
└─────────────────────────────────────────────────────────────────┘
```

### Parameter Server (`ps`)

**Role:** Store model weights and aggregate gradients

```
┌─────────────────────────────────────────────────────────────────┐
│                    PARAMETER SERVER TASK                         │
│                                                                  │
│   1. Initialize and store model weights                         │
│   2. Send weights to workers on request                         │
│   3. Receive gradients from ALL workers                         │
│   4. Aggregate: avg_grad = sum(gradients) / num_workers         │
│   5. Update: weights = weights - learning_rate * avg_grad       │
│   6. Repeat                                                      │
│                                                                  │
│   Resources: No GPU, High CPU, Very High Memory, High Network   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Can Tasks Run in Parallel?

### Answer: YES! Tasks within a job run IN PARALLEL.

```
┌─────────────────────────────────────────────────────────────────┐
│                           JOB                                    │
│                                                                  │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │ TASK: "worker"          TASK: "ps"                      │   │
│   │ (8 instances)           (2 instances)                   │   │
│   │                                                          │   │
│   │  ┌───┐┌───┐┌───┐┌───┐    ┌────┐┌────┐                  │   │
│   │  │W0 ││W1 ││W2 ││W3 │    │PS0 ││PS1 │                  │   │
│   │  └───┘└───┘└───┘└───┘    └────┘└────┘                  │   │
│   │  ┌───┐┌───┐┌───┐┌───┐                                   │   │
│   │  │W4 ││W5 ││W6 ││W7 │    ALL RUNNING AT THE            │   │
│   │  └───┘└───┘└───┘└───┘    SAME TIME (PARALLEL!)         │   │
│   │                                                          │   │
│   └─────────────────────────────────────────────────────────┘   │
│                                                                  │
│   Total: 10 instances running in parallel                       │
└─────────────────────────────────────────────────────────────────┘
```

### Parallelism Summary

| Level | Parallel? | Explanation |
|-------|-----------|-------------|
| **Jobs** | NO (Sequential) | Job 1 finishes → Job 2 starts |
| **Tasks within Job** | YES (Parallel) | Worker task + PS task run together |
| **Instances within Task** | YES (Parallel) | Worker-0, Worker-1, ... all run together |

---

## Why Tasks MUST Run in Parallel

Distributed training **requires** tasks to run simultaneously:

```
┌─────────────────────────────────────────────────────────────────┐
│                    TRAINING STEP TIMELINE                        │
│                                                                  │
│   TIME ────────────────────────────────────────────────────▶    │
│                                                                  │
│   PS:     [  wait for gradients  ][aggregate][  send weights  ] │
│                     ▲       ▲                      │      │     │
│                     │       │                      ▼      ▼     │
│   Worker0: [forward][backward][send grad]    [receive][forward] │
│   Worker1: [forward][backward][send grad]    [receive][forward] │
│   Worker2: [forward][backward][send grad]    [receive][forward] │
│   Worker3: [forward][backward][send grad]    [receive][forward] │
│                                                                  │
│   ◀──────────── One Training Step ────────────▶                 │
│                                                                  │
│   ALL tasks must be running at the same time!                   │
└─────────────────────────────────────────────────────────────────┘
```

**If tasks ran sequentially (hypothetically):**
```
Worker0 runs → Worker1 runs → Worker2 runs → PS runs
     ↑
     This would be 4x slower and PS would have nothing to aggregate!
```

---

## Gang Scheduling

Because tasks MUST run together, schedulers use **Gang Scheduling**:

```
┌─────────────────────────────────────────────────────────────────┐
│                      GANG SCHEDULING                             │
│                                                                  │
│   Job requests: 8 workers (8 GPUs) + 2 PS (0 GPUs)              │
│                                                                  │
│   ✗ WRONG: Start 4 workers now, 4 workers later                 │
│            → Training cannot proceed with partial workers        │
│                                                                  │
│   ✓ CORRECT: Wait until ALL 8 GPUs available, then start ALL   │
│              → All workers + PS start together                   │
│                                                                  │
│   Scheduler waits:                                               │
│   ┌─────────────────────────────────────────────┐               │
│   │ Time 0: Only 4 GPUs free → Job waits       │               │
│   │ Time 1: Only 6 GPUs free → Job waits       │               │
│   │ Time 2: 8 GPUs free → START ALL TOGETHER!  │               │
│   └─────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────┘
```

---

## Communication Patterns

### Pattern 1: Parameter Server (PS) Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    PS ARCHITECTURE                               │
│                                                                  │
│                      ┌──────────┐                               │
│                      │    PS    │  ← Stores weights             │
│                      │ (no GPU) │                               │
│                      └────┬─────┘                               │
│                  ┌────────┼────────┐                            │
│                  │        │        │                             │
│                  ▼        ▼        ▼                             │
│             ┌────────┐┌────────┐┌────────┐                      │
│             │Worker 0││Worker 1││Worker 2│  ← Compute gradients │
│             │  GPU   ││  GPU   ││  GPU   │                      │
│             └────────┘└────────┘└────────┘                      │
│                                                                  │
│   Tasks: ["worker", "ps"]                                       │
│   Communication: All workers ↔ PS (star topology)               │
└─────────────────────────────────────────────────────────────────┘
```

### Pattern 2: AllReduce (No PS)

```
┌─────────────────────────────────────────────────────────────────┐
│                   ALLREDUCE ARCHITECTURE                         │
│                                                                  │
│             ┌────────┐         ┌────────┐                       │
│             │Worker 0│◀───────▶│Worker 1│                       │
│             │  GPU   │         │  GPU   │                       │
│             └────┬───┘         └───┬────┘                       │
│                  │      ╲   ╱      │                            │
│                  │       ╲ ╱       │                            │
│                  │        ╳        │                            │
│                  │       ╱ ╲       │                            │
│                  │      ╱   ╲      │                            │
│             ┌────┴───┐         ┌───┴────┐                       │
│             │Worker 2│◀───────▶│Worker 3│                       │
│             │  GPU   │         │  GPU   │                       │
│             └────────┘         └────────┘                       │
│                                                                  │
│   Tasks: ["worker"] only (no PS!)                               │
│   Communication: Workers directly (ring topology)               │
│   More efficient for large models                               │
└─────────────────────────────────────────────────────────────────┘
```

---

## Example: Job with Multiple Tasks

### In Alibaba Trace Data

```
pai_job_table.csv:
┌─────────────┬──────────┬────────────┐
│ job_name    │ user     │ status     │
├─────────────┼──────────┼────────────┤
│ bert_train_1│ alice    │ Terminated │
└─────────────┴──────────┴────────────┘

pai_task_table.csv:
┌─────────────┬────────────────┬──────────┬──────────┬──────────┐
│ job_name    │ task_name      │ inst_num │ plan_gpu │ plan_cpu │
├─────────────┼────────────────┼──────────┼──────────┼──────────┤
│ bert_train_1│ xtensorflow    │ 8        │ 100.0    │ 800.0    │ ← 8 workers
│ bert_train_1│ ps             │ 2        │ 0.0      │ 1600.0   │ ← 2 PS
└─────────────┴────────────────┴──────────┴──────────┴──────────┘

pai_instance_table.csv:
┌─────────────┬────────────────┬────────────┬─────────────┐
│ job_name    │ task_name      │ inst_name  │ machine     │
├─────────────┼────────────────┼────────────┼─────────────┤
│ bert_train_1│ xtensorflow    │ worker-0   │ machine_A   │ ┐
│ bert_train_1│ xtensorflow    │ worker-1   │ machine_B   │ │
│ bert_train_1│ xtensorflow    │ worker-2   │ machine_C   │ │ 8 workers
│ bert_train_1│ xtensorflow    │ worker-3   │ machine_D   │ │ running in
│ bert_train_1│ xtensorflow    │ worker-4   │ machine_E   │ │ PARALLEL
│ bert_train_1│ xtensorflow    │ worker-5   │ machine_F   │ │
│ bert_train_1│ xtensorflow    │ worker-6   │ machine_G   │ │
│ bert_train_1│ xtensorflow    │ worker-7   │ machine_H   │ ┘
│ bert_train_1│ ps             │ ps-0       │ machine_X   │ ┐ 2 PS running
│ bert_train_1│ ps             │ ps-1       │ machine_Y   │ ┘ in PARALLEL
└─────────────┴────────────────┴────────────┴─────────────┘

TOTAL: 10 instances running in PARALLEL on 10 different machines
```

---

## Parallelism Levels - Complete Picture

```
┌─────────────────────────────────────────────────────────────────┐
│                    PARALLELISM HIERARCHY                         │
│                                                                  │
│  LEVEL 1: JOBS (Sequential)                                     │
│  ─────────────────────────                                      │
│  Job A ────▶ Job B ────▶ Job C                                  │
│  (finish)   (start)     (wait)                                  │
│                                                                  │
│  LEVEL 2: TASKS within Job (Parallel)                           │
│  ────────────────────────────────────                           │
│  ┌─────────────── Job A ───────────────┐                        │
│  │  Task:worker ◀──────▶ Task:ps       │  ← Run simultaneously  │
│  └─────────────────────────────────────┘                        │
│                                                                  │
│  LEVEL 3: INSTANCES within Task (Parallel)                      │
│  ─────────────────────────────────────────                      │
│  ┌─────────────── Task:worker ─────────────────┐                │
│  │  [W0] [W1] [W2] [W3] [W4] [W5] [W6] [W7]   │ ← All parallel │
│  └─────────────────────────────────────────────┘                │
│                                                                  │
│  LEVEL 4: DATA within Instance (Parallel - GPU cores)           │
│  ────────────────────────────────────────────────               │
│  ┌─────────────── Instance W0 ─────────────────┐                │
│  │  GPU cores process batch data in parallel   │                │
│  └─────────────────────────────────────────────┘                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Why Jobs are Sequential (Not Parallel)

**Q: Can multiple jobs from same user run in parallel?**

**A: Yes, but usually they don't need to for ONE training run.**

```
┌─────────────────────────────────────────────────────────────────┐
│  SAME TRAINING RUN: Jobs are sequential (checkpointing)         │
│                                                                  │
│  Job 1 (steps 0-10K) → Job 2 (steps 10K-20K) → Job 3 (20K-30K) │
│                                                                  │
│  Why not one big job?                                            │
│  - Cluster time limits (max 24 hours)                           │
│  - Fault tolerance (restart from checkpoint)                    │
│  - Resource reallocation between jobs                           │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  DIFFERENT EXPERIMENTS: Jobs CAN be parallel                    │
│                                                                  │
│  User Alice submits:                                            │
│  - Job A: BERT with lr=0.001  ─┐                                │
│  - Job B: BERT with lr=0.0001 ─┼─▶ Can run in parallel!        │
│  - Job C: BERT with lr=0.01   ─┘   (if cluster has resources)   │
│                                                                  │
│  These are independent experiments, not one training run        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Summary

| Question | Answer |
|----------|--------|
| Do tasks within a job run in parallel? | **YES** - Required for distributed training |
| Do instances within a task run in parallel? | **YES** - Data parallelism |
| Do jobs run in parallel? | **Usually NO** for same training, **YES** for different experiments |
| Why must tasks run together? | Workers and PS must communicate in real-time |
| What is gang scheduling? | Start all instances together or none |
