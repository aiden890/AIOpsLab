# AIOpsLab Workload Generator Documentation

This document explains the workload generation system in AIOpsLab, including what workload means, how it works, and how to customize it.

---

## What is Workload?

**Workload** in AIOpsLab refers to simulated user traffic sent to microservice applications. It serves several purposes:

| Purpose | Description |
|---------|-------------|
| **Realistic Testing** | Simulates real user behavior (search, login, purchase, etc.) |
| **Fault Manifestation** | Makes faults visible through errors, latency, or failures |
| **Telemetry Generation** | Produces logs, metrics, and traces for agent analysis |
| **Stress Testing** | Tests system behavior under various load conditions |

### Why Workload Matters

Without workload, a faulty system might appear healthy because:
- No requests = No errors to observe
- No traffic = No latency spikes
- No user actions = No trace data

Workload makes problems **observable** by exercising the system.

---

## wrk2 Tool

AIOpsLab uses **wrk2**, an HTTP benchmarking tool with constant throughput and latency recording.

### Key Features

| Feature | Description |
|---------|-------------|
| **Constant Rate** | Maintains specified requests per second |
| **Lua Scripting** | Customizable request generation via Lua scripts |
| **Latency Recording** | Records detailed latency distribution |
| **Multi-threaded** | Supports concurrent connections and threads |

### wrk2 vs wrk

| Tool | Description |
|------|-------------|
| **wrk** | Original - max throughput mode |
| **wrk2** | Modified - constant throughput mode (better for realistic testing) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster                        │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                wrk2 Job (Pod)                        │    │
│  │  ┌─────────────┐    ┌─────────────────────────────┐ │    │
│  │  │ wrk2 binary │ ←→ │ Lua Script (ConfigMap)      │ │    │
│  │  │             │    │ - Request generation        │ │    │
│  │  │             │    │ - API endpoint selection    │ │    │
│  │  └─────────────┘    └─────────────────────────────┘ │    │
│  └───────────────────────────┬─────────────────────────┘    │
│                              │ HTTP Requests                 │
│                              ▼                               │
│  ┌─────────────────────────────────────────────────────┐    │
│  │           Target Application (DeathStarBench)        │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐          │    │
│  │  │ frontend │→ │ service  │→ │ database │          │    │
│  │  └──────────┘  └──────────┘  └──────────┘          │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

---

## Wrk Class (Python Interface)

### Location

```
aiopslab/generators/workload/wrk.py
```

### Class Definition

```python
class Wrk:
    def __init__(
        self,
        rate,           # Requests per second
        dist="norm",    # Distribution: "norm", "exp", "fixed"
        connections=2,  # Number of TCP connections
        duration=6,     # Duration in seconds
        threads=2,      # Number of threads
        latency=True    # Record latency distribution
    ):
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `rate` | int | - | Target requests per second |
| `dist` | str | "norm" | Request distribution (`norm`, `exp`, `fixed`) |
| `connections` | int | 2 | Number of concurrent TCP connections |
| `duration` | int | 6 | Test duration in seconds |
| `threads` | int | 2 | Number of worker threads |
| `latency` | bool | True | Enable latency recording |

### Distribution Types

| Distribution | Description | Use Case |
|--------------|-------------|----------|
| `norm` | Normal distribution | Typical user behavior |
| `exp` | Exponential distribution | Bursty traffic patterns |
| `fixed` | Fixed interval | Precise rate control |

### Methods

```python
# Start workload with a Lua script targeting a URL
wrk.start_workload(payload_script, url)

# Create ConfigMap for Lua script
wrk.create_configmap(name, namespace, payload_script_path)

# Create Kubernetes Job for wrk2
wrk.create_wrk_job(job_name, namespace, payload_script, url)
```

---

## Lua Scripts

Lua scripts define **what requests to generate**. They're the "payload" that determines workload behavior.

### Script Location

```
aiopslab-applications/
├── hotelReservation/wrk2/scripts/
│   └── hotel-reservation/
│       └── mixed-workload_type_1.lua
└── socialNetwork/wrk2/scripts/
    └── social-network/
        ├── mixed-workload.lua
        ├── compose-post.lua
        ├── read-home-timeline.lua
        └── read-user-timeline.lua
```

### Script Structure

Every Lua script must define a `request()` function:

```lua
-- Required: This function is called for each request
request = function()
    -- Return a formatted HTTP request
    return wrk.format(method, path, headers, body)
end
```

### Basic Example

```lua
local socket = require("socket")
math.randomseed(socket.gettime()*1000)

request = function()
    local method = "GET"
    local path = "http://localhost:5000/health"
    local headers = {}
    return wrk.format(method, path, headers, nil)
end
```

---

## Existing Workload Scripts

### Hotel Reservation - Mixed Workload

**File:** `hotelReservation/wrk2/scripts/hotel-reservation/mixed-workload_type_1.lua`

**Request Distribution:**

| Operation | Ratio | Description |
|-----------|-------|-------------|
| `search_hotel()` | 60% | Search for available hotels |
| `recommend()` | 39% | Get hotel recommendations |
| `user_login()` | 0.5% | User login |
| `reserve()` | 0.5% | Make reservation |

**Code:**

```lua
request = function()
    local search_ratio      = 0.6
    local recommend_ratio   = 0.39
    local user_ratio        = 0.005
    local reserve_ratio     = 0.005

    local coin = math.random()
    if coin < search_ratio then
        return search_hotel()
    elseif coin < search_ratio + recommend_ratio then
        return recommend()
    elseif coin < search_ratio + recommend_ratio + user_ratio then
        return user_login()
    else
        return reserve()
    end
end
```

---

### Social Network - Mixed Workload

**File:** `socialNetwork/wrk2/scripts/social-network/mixed-workload.lua`

**Request Distribution:**

| Operation | Ratio | Description |
|-----------|-------|-------------|
| `read_home_timeline()` | 60% | Read home timeline feed |
| `read_user_timeline()` | 30% | Read user's own timeline |
| `compose_post()` | 10% | Create a new post |

**Code:**

```lua
request = function()
    local read_home_timeline_ratio = 0.60
    local read_user_timeline_ratio = 0.30
    local compose_post_ratio       = 0.10

    local coin = math.random()
    if coin < read_home_timeline_ratio then
        return read_home_timeline()
    elseif coin < read_home_timeline_ratio + read_user_timeline_ratio then
        return read_user_timeline()
    else
        return compose_post()
    end
end
```

---

## How to Create Custom Workload

### Step 1: Create a Lua Script

```lua
-- custom-workload.lua
local socket = require("socket")
math.randomseed(socket.gettime()*1000)

-- Define your API endpoints
local function api_endpoint_1()
    local method = "GET"
    local path = "http://localhost:8080/api/endpoint1?param=value"
    local headers = {}
    return wrk.format(method, path, headers, nil)
end

local function api_endpoint_2()
    local method = "POST"
    local path = "http://localhost:8080/api/endpoint2"
    local headers = {}
    headers["Content-Type"] = "application/json"
    local body = '{"key": "value"}'
    return wrk.format(method, path, headers, body)
end

-- Define request distribution
request = function()
    local ratio_1 = 0.7  -- 70% endpoint1
    local ratio_2 = 0.3  -- 30% endpoint2

    local coin = math.random()
    if coin < ratio_1 then
        return api_endpoint_1()
    else
        return api_endpoint_2()
    end
end
```

### Step 2: Use in Python

```python
from pathlib import Path
from aiopslab.generators.workload.wrk import Wrk

# Create workload generator
wrk = Wrk(
    rate=100,        # 100 requests/second
    dist="exp",      # Exponential distribution
    connections=10,  # 10 connections
    duration=60,     # Run for 60 seconds
    threads=4        # Use 4 threads
)

# Start workload
payload_script = Path("/path/to/custom-workload.lua")
wrk.start_workload(
    payload_script=payload_script,
    url="http://frontend-service:8080"
)
```

### Step 3: Use in Problem Definition

```python
class MyCustomProblem:
    def __init__(self):
        self.payload_script = Path("path/to/custom-workload.lua")

    def start_workload(self):
        print("== Start Workload ==")
        frontend_url = get_frontend_url(self.app)

        wrk = Wrk(rate=50, dist="exp", connections=5, duration=30, threads=2)
        wrk.start_workload(
            payload_script=self.payload_script,
            url=frontend_url,
        )
```

---

## Workload Examples for Specific Purposes

### Example 1: High Load Stress Test

```python
# Stress test with high request rate
wrk = Wrk(
    rate=1000,       # 1000 req/s
    dist="fixed",    # Constant rate
    connections=100, # Many connections
    duration=120,    # 2 minutes
    threads=8        # High parallelism
)
```

### Example 2: Bursty Traffic Pattern

```python
# Simulate bursty user traffic
wrk = Wrk(
    rate=200,        # Average 200 req/s
    dist="exp",      # Exponential = bursty
    connections=20,
    duration=60,
    threads=4
)
```

### Example 3: Light Background Load

```python
# Light load for monitoring
wrk = Wrk(
    rate=10,         # Low rate
    dist="norm",     # Normal distribution
    connections=2,
    duration=300,    # 5 minutes
    threads=2
)
```

### Example 4: Read-Heavy Workload Script

```lua
-- read-heavy-workload.lua
request = function()
    local read_ratio = 0.95   -- 95% reads
    local write_ratio = 0.05  -- 5% writes

    local coin = math.random()
    if coin < read_ratio then
        return read_operation()
    else
        return write_operation()
    end
end
```

### Example 5: Write-Heavy Workload Script

```lua
-- write-heavy-workload.lua
request = function()
    local read_ratio = 0.20   -- 20% reads
    local write_ratio = 0.80  -- 80% writes

    local coin = math.random()
    if coin < read_ratio then
        return read_operation()
    else
        return write_operation()
    end
end
```

### Example 6: Single Endpoint Focus

```lua
-- focus-recommendation.lua
-- Target only the recommendation service for testing
request = function()
    local req_param = "rate"  -- Always request by rate
    local lat = 38.0235
    local lon = -122.095

    local method = "GET"
    local path = "http://localhost:5000/recommendations?require=" .. req_param ..
        "&lat=" .. tostring(lat) .. "&lon=" .. tostring(lon)
    return wrk.format(method, path, {}, nil)
end
```

---

## Kubernetes Job Template

**File:** `aiopslab/generators/workload/wrk-job-template.yaml`

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: wrk2-job
spec:
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: wrk2
          image: deathstarbench/wrk2-client:latest
          args: []  # Populated by Python code
          volumeMounts:
            - name: wrk2-scripts
              mountPath: /scripts
              readOnly: true
      volumes:
        - name: wrk2-scripts
          configMap:
            name: wrk2-payload-script
```

### Generated Command

The Python code generates this wrk2 command:

```bash
wrk -D exp -t 2 -c 2 -d 10s -L -s /scripts/mixed-workload.lua http://frontend:5000 -R 100 --latency
```

| Flag | Description |
|------|-------------|
| `-D exp` | Distribution type |
| `-t 2` | 2 threads |
| `-c 2` | 2 connections |
| `-d 10s` | 10 second duration |
| `-L` | Enable latency recording |
| `-s /scripts/...` | Lua script path |
| `-R 100` | 100 requests/second |
| `--latency` | Print latency statistics |

---

## Monitoring Workload

```bash
# Check workload job status
kubectl get jobs -n default

# View workload pod logs
kubectl logs -l job-name=wrk2-job

# Watch job progress
kubectl get pods -l job-name=wrk2-job -w

# Get job details
kubectl describe job wrk2-job
```

---

## File Structure

```
aiopslab/
└── generators/
    └── workload/
        ├── __init__.py
        ├── wrk.py                    # Python Wrk class
        └── wrk-job-template.yaml     # K8s job template

aiopslab-applications/
├── hotelReservation/
│   └── wrk2/scripts/
│       └── hotel-reservation/
│           └── mixed-workload_type_1.lua
└── socialNetwork/
    └── wrk2/scripts/
        └── social-network/
            ├── mixed-workload.lua
            ├── compose-post.lua
            ├── read-home-timeline.lua
            └── read-user-timeline.lua
```
