# AIOpsLab Task Types Documentation

This document explains the four task types in AIOpsLab and their solution formats.

---

## Overview

AIOpsLab defines four main task types that represent different stages of incident response:

| Task | File | Metric | Description |
|------|------|--------|-------------|
| **Detection** | `detection.py` | TTD (Time To Detect) | Detect if anomalies exist |
| **Localization** | `localization.py` | TTL (Time To Localize) | Find faulty components |
| **Analysis** | `analysis.py` | TTA (Time To Analyze) | Determine root cause type |
| **Mitigation** | `mitigation.py` | TTM (Time To Mitigate) | Fix the problem |

### Task Flow

```
┌────────────┐    ┌──────────────┐    ┌────────────┐    ┌────────────┐
│ Detection  │ →  │ Localization │ →  │  Analysis  │ →  │ Mitigation │
│ "Is there  │    │ "Where is    │    │ "What type │    │ "Fix the   │
│  a fault?" │    │  the fault?" │    │  of fault?"│    │  problem"  │
└────────────┘    └──────────────┘    └────────────┘    └────────────┘
     TTD               TTL                 TTA               TTM
```

---

## Available APIs (Actions)

All task types share these common APIs:

### Telemetry APIs

| API | Description |
|-----|-------------|
| `get_logs(namespace, service)` | Collect logs from a pod/container |
| `get_metrics(namespace, duration)` | Collect Prometheus metrics |
| `read_metrics(file_path)` | Read metrics from CSV file |
| `get_traces(namespace, duration)` | Collect Jaeger traces |
| `read_traces(file_path)` | Read traces from CSV file |

### Shell API

| API | Description |
|-----|-------------|
| `exec_shell(command, timeout=30)` | Execute shell command (non-interactive) |

**Blocked Commands:**
- `kubectl edit` → Use `kubectl patch` instead
- `kubectl port-forward` → Interactive, not allowed
- `docker logs -f` / `kubectl logs -f` → Use without `-f` flag

### Submit API

Each task type has its own `submit()` format (see below).

---

## Task 1: Detection

**Goal:** Determine if there are any anomalies in the system.

**Metric:** TTD (Time To Detect)

### Solution Format

```python
# If anomalies are detected:
submit("Yes")

# If no anomalies:
submit("No")
```

### Example Agent Response

```
Thought: I checked the logs and found error messages indicating service failures.
Action: ```
submit("Yes")
```
```

---

## Task 2: Localization

**Goal:** Identify which service(s) contain the root cause of the fault.

**Metric:** TTL (Time To Localize)

### Solution Format

```python
# If faulty components are found:
submit(["service-name-1", "service-name-2"])

# If no faults are found:
submit([])
```

### Example Agent Response

```
Thought: The traces show that the 'recommendation' service has high latency and errors.
Action: ```
submit(["recommendation"])
```
```

### Valid Service Names

For Hotel Reservation:
```
frontend, geo, profile, rate, recommendation, reservation, search, user
mongodb-geo, mongodb-profile, mongodb-rate, mongodb-recommendation, mongodb-reservation, mongodb-user
memcached-profile, memcached-rate, memcached-reserve
consul, jaeger
```

For Social Network:
```
nginx-thrift, compose-post-service, home-timeline-service, user-timeline-service
post-storage-service, social-graph-service, user-service, user-mention-service
text-service, unique-id-service, url-shorten-service, media-service, media-frontend
post-storage-mongodb, social-graph-mongodb, user-mongodb, user-timeline-mongodb, url-shorten-mongodb, media-mongodb
home-timeline-redis, social-graph-redis, user-timeline-redis
post-storage-memcached, user-memcached, url-shorten-memcached, media-memcached
jaeger
```

---

## Task 3: Analysis (Root Cause Analysis)

**Goal:** Determine the system level and type of fault.

**Metric:** TTA (Time To Analyze)

### Solution Format

```python
# If fault is detected:
submit({
    "system_level": "<level>",
    "fault_type": "<type>"
})

# If no fault is detected:
submit()
```

### System Level Options

| Value | Description |
|-------|-------------|
| `Hardware` | Physical hardware failures |
| `Operating System` | OS-level issues (kernel, drivers) |
| `Virtualization` | Container/VM layer issues |
| `Application` | Application code/config issues |

### Fault Type Options

| Value | Description |
|-------|-------------|
| `Misconfiguration` | Wrong configuration settings |
| `Code Defect` | Bugs in application code |
| `Authentication Issue` | Auth/permission problems |
| `Network/Storage Issue` | Network or storage failures |
| `Operation Error` | Human operational mistakes |
| `Dependency Problem` | Issues with dependencies |

### Example Agent Response

```
Thought: The MongoDB authentication is failing due to revoked credentials.
Action: ```
submit({"system_level": "Application", "fault_type": "Authentication Issue"})
```
```

---

## Task 4: Mitigation

**Goal:** Fix the problem and restore the system to normal operation.

**Metric:** TTM (Time To Mitigate)

### Solution Format

```python
# After performing mitigation actions:
submit()
```

**Note:** The `submit()` call takes no parameters. The agent should perform the actual mitigation using `exec_shell()` or other APIs before calling `submit()`.

### Example Agent Response

```
Thought: I need to restart the failed pod to recover the service.
Action: ```
exec_shell("kubectl delete pod recommendation-xxx -n test-hotel-reservation")
```

... (after pod recovers) ...

Thought: The pod has been restarted and the service is healthy now.
Action: ```
submit()
```
```

### Common Mitigation Actions

```bash
# Restart a pod
kubectl delete pod <pod-name> -n <namespace>

# Scale deployment
kubectl scale deployment <name> --replicas=<n> -n <namespace>

# Patch configuration
kubectl patch deployment <name> -n <namespace> -p '{"spec":...}'

# Rollback deployment
kubectl rollout undo deployment/<name> -n <namespace>

# Apply configuration fix
kubectl apply -f <config-file>
```

### How Mitigation is Evaluated

**The system checks the actual state of the Kubernetes cluster after `submit()` is called.**

The agent doesn't tell the system what it fixed. Instead, the system independently verifies the cluster state:

```python
def eval(self, soln, trace, duration):
    # Check if ALL pods are healthy after mitigation
    pod_list = self.kubectl.list_pods(self.namespace)
    all_normal = True

    for pod in pod_list.items:
        for container_status in pod.status.container_statuses:
            # Check 1: Not in CrashLoopBackOff
            if container_status.state.waiting and \
               container_status.state.waiting.reason == "CrashLoopBackOff":
                all_normal = False

            # Check 2: Not terminated abnormally
            elif container_status.state.terminated and \
                 container_status.state.terminated.reason != "Completed":
                all_normal = False

            # Check 3: Container is ready
            elif not container_status.ready:
                all_normal = False

    self.results["success"] = all_normal  # True/False
```

#### Evaluation Checks

| Check | Condition | Pass |
|-------|-----------|------|
| **CrashLoopBackOff** | No pod in CrashLoopBackOff | ✓ |
| **Terminated** | No abnormal termination | ✓ |
| **Ready** | All containers ready | ✓ |
| **Problem-specific** | Depends on fault type | ✓ |

#### Problem-Specific Checks

Some problems have additional verification:

```python
# Example: ScalePod problem also checks replica count
deployment = self.kubectl.get_deployment(faulty_service, namespace)
if deployment.spec.replicas != 1 or deployment.status.available_replicas != 1:
    all_normal = False
```

#### Evaluation Flow

```
Agent performs mitigation actions (exec_shell, etc.)
         │
         ▼
   ┌─────────────┐
   │  submit()   │
   └─────────────┘
         │
         ▼
   ┌─────────────────────────────────┐
   │     eval() is called            │
   │  - Check pod statuses           │
   │  - Check container readiness    │
   │  - Problem-specific checks      │
   └─────────────────────────────────┘
         │
         ▼
   ┌─────────────┐
   │ success:    │
   │ True/False  │
   └─────────────┘
```

This approach is more robust than trusting the agent's self-reported solution!

---

## Response Format

All API calls must be in a markdown code block:

```
```
<API_NAME>(<API_PARAM1>, <API_PARAM2>, ...)
```
```

### Correct Examples

```
```
exec_shell("kubectl get pods -n test-hotel-reservation")
```
```

```
```
get_logs("test-hotel-reservation", "recommendation")
```
```

```
```
submit(["recommendation"])
```
```

### Incorrect Examples

```
# Wrong: Missing code block
exec_shell("kubectl get pods")

# Wrong: Multiple API calls
```
exec_shell("cmd1")
exec_shell("cmd2")
```

# Wrong: Extra text
Let me check the pods:
```
exec_shell("kubectl get pods")
```
```

---

## Evaluation Metrics

### Quantitative Metrics

| Metric | Description |
|--------|-------------|
| `TTD` / `TTL` / `TTA` / `TTM` | Time to complete the task (seconds) |
| `steps` | Number of actions taken |
| `in_tokens` | Input tokens consumed |
| `out_tokens` | Output tokens generated |

### Qualitative Metrics (Optional)

| Metric | Description |
|--------|-------------|
| `reasoning_score` | LLM judge score for reasoning quality |
| `reasoning_judgement` | LLM judge explanation |

---

## Task Implementation Files

```
aiopslab/orchestrator/tasks/
├── base.py          # Base Task class
├── detection.py     # DetectionTask
├── localization.py  # LocalizationTask
├── analysis.py      # AnalysisTask
└── mitigation.py    # MitigationTask

aiopslab/orchestrator/actions/
├── base.py          # TaskActions (common APIs)
├── detection.py     # DetectionActions (submit)
├── localization.py  # LocalizationActions (submit)
├── analysis.py      # AnalysisActions (submit)
└── mitigation.py    # MitigationActions (submit)
```
