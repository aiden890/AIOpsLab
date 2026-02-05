# AIOpsLab Actions (APIs) Documentation

This document describes all available actions (APIs) that agents can use in AIOpsLab.

---

## Overview

Actions are the interface between agents and the AIOpsLab environment. Each action is a function that the agent can call to interact with the system.

### Action Categories

| Category | Description |
|----------|-------------|
| **Telemetry APIs** | Collect observability data (logs, metrics, traces) |
| **Shell API** | Execute shell commands |
| **Submit API** | Submit solutions for evaluation |

### Action Types

| Decorator | Type | Description |
|-----------|------|-------------|
| `@read` | Read | Read-only actions (telemetry collection) |
| `@write` | Write | Actions that modify system state |
| `@action` | General | General actions (shell, submit) |

---

## Telemetry APIs

### get_logs

Collects log data from a pod (Kubernetes) or container (Docker).

```python
get_logs(namespace: str, service: str) -> str
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `namespace` | str | Namespace where service runs (`test-hotel-reservation`, `test-social-network`, `docker`) |
| `service` | str | Name of the service |

**Returns:** Log data as a string (deduplicated for readability)

**Example:**

```
get_logs("test-hotel-reservation", "recommendation")
```

**Service Label Mapping:**

| Namespace | Label Selector |
|-----------|----------------|
| `test-social-network` | `app={service}` |
| `test-hotel-reservation` | `io.kompose.service={service}` |
| `astronomy-shop` | `app.kubernetes.io/name={service}` |
| `docker` | Container name |

---

### get_metrics

Collects metrics data from Prometheus.

```python
get_metrics(namespace: str, duration: int = 5) -> str
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `namespace` | str | - | Namespace to collect metrics from |
| `duration` | int | 5 | Minutes of metrics to collect (from now - duration to now) |

**Returns:** Path to directory where metrics CSV files are saved

**Example:**

```
get_metrics("test-hotel-reservation", 10)
```

**Output:** `metrics_output/` directory with CSV files

---

### read_metrics

Reads metrics from a CSV file.

```python
read_metrics(file_path: str) -> str
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `file_path` | str | Path to the metrics CSV file |

**Returns:** Metrics data as a formatted string

**Example:**

```
read_metrics("metrics_output/cpu_usage.csv")
```

---

### get_traces

Collects distributed trace data from Jaeger.

```python
get_traces(namespace: str, duration: int = 5) -> str
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `namespace` | str | - | Namespace to collect traces from |
| `duration` | int | 5 | Minutes of traces to collect |

**Returns:** Path to directory where trace CSV files are saved

**Example:**

```
get_traces("test-hotel-reservation", 5)
```

**Output:** `trace_output/` directory with CSV files

---

### read_traces

Reads traces from a CSV file.

```python
read_traces(file_path: str) -> str
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `file_path` | str | Path to the traces CSV file |

**Returns:** Trace data as a formatted string

**Example:**

```
read_traces("trace_output/traces.csv")
```

---

## Shell API

### exec_shell

Executes any shell command in the debugging environment.

```python
exec_shell(command: str, timeout: int = 30) -> str
```

**Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | str | - | Shell command to execute |
| `timeout` | int | 30 | Timeout in seconds |

**Returns:** Command output as a string

**Example:**

```
exec_shell("kubectl get pods -n test-hotel-reservation")
```

**Important Notes:**

- This is NOT a stateful or interactive shell session
- Log outputs are automatically deduplicated for readability

### Blocked Commands

| Command | Error Message | Alternative |
|---------|---------------|-------------|
| `kubectl edit` | Cannot use `kubectl edit` | Use `kubectl patch` |
| `edit svc` | Cannot use `kubectl edit` | Use `kubectl patch` |
| `kubectl port-forward` | Interactive command not allowed | - |
| `docker logs -f` | Cannot use `-f` flag | Use `docker logs` |
| `kubectl logs -f` | Cannot use `-f` flag | Use `kubectl logs` |

### Common Shell Commands

```bash
# List pods
kubectl get pods -n <namespace>

# Describe pod
kubectl describe pod <pod-name> -n <namespace>

# Get pod logs
kubectl logs <pod-name> -n <namespace>

# Get events
kubectl get events -n <namespace> --sort-by='.lastTimestamp'

# Check deployments
kubectl get deployments -n <namespace>

# Patch deployment
kubectl patch deployment <name> -n <namespace> -p '{"spec":...}'

# Scale deployment
kubectl scale deployment <name> --replicas=<n> -n <namespace>

# Delete pod (for restart)
kubectl delete pod <pod-name> -n <namespace>

# Get services
kubectl get svc -n <namespace>

# Get configmaps
kubectl get configmaps -n <namespace>

# Docker commands (for docker namespace)
docker ps
docker logs <container>
docker inspect <container>
```

---

## Submit APIs

Each task type has its own submit function with a different signature.

### Detection Submit

```python
submit(has_anomaly: str) -> SubmissionStatus
```

**Parameter:** `"Yes"` or `"No"`

**Example:**

```
submit("Yes")
```

---

### Localization Submit

```python
submit(faulty_components: list[str]) -> SubmissionStatus
```

**Parameter:** List of faulty service names, or empty list

**Example:**

```
submit(["recommendation", "mongodb-recommendation"])
```

---

### Analysis Submit

```python
submit(analysis: dict[str, str]) -> SubmissionStatus
```

**Parameter:** Dictionary with `system_level` and `fault_type`

**System Level Options:**
- `Hardware`
- `Operating System`
- `Virtualization`
- `Application`

**Fault Type Options:**
- `Misconfiguration`
- `Code Defect`
- `Authentication Issue`
- `Network/Storage Issue`
- `Operation Error`
- `Dependency Problem`

**Example:**

```
submit({"system_level": "Application", "fault_type": "Authentication Issue"})
```

---

### Mitigation Submit

```python
submit() -> SubmissionStatus
```

**Parameter:** None

**Example:**

```
submit()
```

---

## Actions by Task Type

| Action | Detection | Localization | Analysis | Mitigation |
|--------|:---------:|:------------:|:--------:|:----------:|
| `get_logs` | ✓ | ✓ | ✓ | ✓ |
| `get_metrics` | ✓ | ✓ | ✓ | ✓ |
| `read_metrics` | ✓ | ✓ | ✓ | ✓ |
| `get_traces` | ✓ | ✓ | ✓ | ✓ |
| `read_traces` | ✓ | ✓ | ✓ | ✓ |
| `exec_shell` | ✓ | ✓ | ✓ | ✓ |
| `submit` | ✓ | ✓ | ✓ | ✓ |

All actions are available for all task types. The difference is in the `submit` function signature.

---

## Response Format

All API calls must be wrapped in a markdown code block:

````
```
<API_NAME>(<param1>, <param2>, ...)
```
````

### Correct Examples

````
```
exec_shell("kubectl get pods -n test-hotel-reservation")
```
````

````
```
get_logs("test-hotel-reservation", "recommendation")
```
````

````
```
submit(["recommendation"])
```
````

### Incorrect Examples

```
# Wrong: No code block
exec_shell("kubectl get pods")

# Wrong: Extra text before/after
Let me check the pods:
```
exec_shell("kubectl get pods")
```

# Wrong: Multiple API calls in one block
```
exec_shell("cmd1")
exec_shell("cmd2")
```
```

---

## Implementation Files

```
aiopslab/orchestrator/actions/
├── base.py          # TaskActions - Common APIs (get_logs, get_metrics, exec_shell, etc.)
├── detection.py     # DetectionActions - submit(has_anomaly: str)
├── localization.py  # LocalizationActions - submit(faulty_components: list[str])
├── analysis.py      # AnalysisActions - submit(analysis: dict[str, str])
└── mitigation.py    # MitigationActions - submit()
```

### Inheritance Structure

```
TaskActions (base.py)
    │
    ├── get_logs()
    ├── get_metrics()
    ├── read_metrics()
    ├── get_traces()
    ├── read_traces()
    └── exec_shell()
         │
         ▼
    ┌────────────────────────────────────────────────┐
    │                                                │
DetectionActions  LocalizationActions  AnalysisActions  MitigationActions
    │                   │                   │              │
    └── submit()        └── submit()        └── submit()   └── submit()
       (str)               (list[str])         (dict)         (None)
```

---

## Tips for Agents

1. **Start with telemetry:** Use `get_logs`, `get_metrics`, `get_traces` to understand the system state
2. **Use exec_shell for flexibility:** When built-in APIs are insufficient, use shell commands
3. **Check pod status first:** `kubectl get pods -n <namespace>` is often a good starting point
4. **Look at events:** `kubectl get events -n <namespace> --sort-by='.lastTimestamp'` shows recent issues
5. **Read logs carefully:** Error messages often indicate the root cause
6. **One action per turn:** Always respond with exactly one API call per turn
