# AIOpsLab Fault Injection Documentation

This document explains the fault injection system in AIOpsLab, including fault types, injectors, and how to create custom faults.

---

## Kubernetes Concepts: Microservice vs Pod

Before diving into fault injection, it's important to understand the relationship between microservices and Kubernetes resources.

### Hierarchy

```
┌─────────────────────────────────────────────────────────────────┐
│                    MICROSERVICE (Logical Concept)                │
│                    e.g., "recommendation"                        │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    DEPLOYMENT (K8s Resource)                     │
│                    Manages desired state                         │
│                    e.g., "recommendation" deployment             │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    REPLICASET (K8s Resource)                     │
│                    Ensures N replicas running                    │
└───────────────────────────┬─────────────────────────────────────┘
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
       ┌─────────┐     ┌─────────┐     ┌─────────┐
       │  POD 1  │     │  POD 2  │     │  POD 3  │
       │(replica)│     │(replica)│     │(replica)│
       └────┬────┘     └────┬────┘     └────┬────┘
            │               │               │
            ▼               ▼               ▼
       ┌─────────┐     ┌─────────┐     ┌─────────┐
       │Container│     │Container│     │Container│
       └─────────┘     └─────────┘     └─────────┘
```

### Key Concepts

| Concept | Description | Example |
|---------|-------------|---------|
| **Microservice** | Logical unit of functionality (not a K8s resource) | "recommendation" service |
| **Deployment** | K8s resource that manages pod lifecycle | `deployment/recommendation` |
| **ReplicaSet** | Ensures specified number of pod replicas | Managed by Deployment |
| **Pod** | Smallest deployable unit, runs container(s) | `recommendation-7d8f9c6b5-abc12` |
| **Container** | Actual running process inside a pod | `hotel-reserv-recommendation` |
| **Service (K8s)** | Network endpoint to access pods | `service/recommendation` |

### Relationship Summary

```
1 Microservice  =  1 Deployment  =  1+ Pods  =  1+ Containers per Pod
```

**Important:**
- A **microservice** can have **multiple pods** (replicas for scaling/availability)
- Each **pod** can have **multiple containers** (sidecar pattern)
- A **K8s Service** provides stable network access to pods (load balancing)

### Example: Hotel Reservation "recommendation" Microservice

```bash
# The microservice "recommendation" consists of:

# 1. Deployment (manages pods)
$ kubectl get deployment recommendation -n test-hotel-reservation
NAME             READY   UP-TO-DATE   AVAILABLE
recommendation   1/1     1            1

# 2. Pod(s) (actual running instances)
$ kubectl get pods -l io.kompose.service=recommendation -n test-hotel-reservation
NAME                              READY   STATUS    RESTARTS
recommendation-7d8f9c6b5-x9z2k    1/1     Running   0

# 3. Service (network endpoint)
$ kubectl get svc recommendation -n test-hotel-reservation
NAME             TYPE        CLUSTER-IP      PORT(S)
recommendation   ClusterIP   10.96.123.45    8085/TCP
```

### How Fault Injection Targets Work

When we say "inject fault into microservice X", we're actually targeting:

| Target | What Happens |
|--------|--------------|
| **Pod** | Kill/pause the specific pod instance |
| **Deployment** | Modify deployment spec (replicas, image, etc.) |
| **Service** | Modify network routing |
| **Container** | Kill specific container in pod |

### Label Selectors

Fault injectors use **labels** to find pods belonging to a microservice:

```yaml
# Hotel Reservation uses this label:
io.kompose.service: recommendation

# Social Network uses this label:
app: user-service
```

**Example in Code:**

```python
# This targets all pods with label "io.kompose.service=recommendation"
chaos_experiment = {
    "spec": {
        "selector": {
            "labelSelectors": {
                "io.kompose.service": "recommendation"
            }
        }
    }
}
```

### Microservice vs Backing Service in Kubernetes

**Important Clarification:** In Kubernetes, both **microservices** (like `recommendation`) and **backing services** (like `mongodb-recommendation`) are deployed the same way!

```
┌─────────────────────────────────────────────────────────────────┐
│                     KUBERNETES VIEW                              │
│            (Everything looks the same in K8s)                    │
│                                                                  │
│  ┌─────────────────────┐      ┌─────────────────────┐           │
│  │ Deployment:         │      │ Deployment:         │           │
│  │ recommendation      │      │ mongodb-recommendation│          │
│  ├─────────────────────┤      ├─────────────────────┤           │
│  │ Pod(s)              │      │ Pod(s)              │           │
│  │ Container(s)        │      │ Container(s)        │           │
│  └─────────────────────┘      └─────────────────────┘           │
│           │                            │                         │
│           ▼                            ▼                         │
│  ┌─────────────────────┐      ┌─────────────────────┐           │
│  │ Service (K8s):      │      │ Service (K8s):      │           │
│  │ recommendation      │      │ mongodb-recommendation│          │
│  │ Port: 8085          │      │ Port: 27017         │           │
│  └─────────────────────┘      └─────────────────────┘           │
│                                                                  │
│      SAME K8s structure, but DIFFERENT architectural role        │
└─────────────────────────────────────────────────────────────────┘
```

**The difference is ARCHITECTURAL, not technical:**

| Aspect | Microservice | Backing Service |
|--------|--------------|-----------------|
| **Example** | `recommendation`, `geo`, `frontend` | `mongodb-recommendation`, `memcached-rate` |
| **K8s Deployment** | ✓ Yes | ✓ Yes |
| **K8s Pod** | ✓ Yes | ✓ Yes |
| **K8s Service** | ✓ Yes | ✓ Yes |
| **Contains business logic** | ✓ Yes (Go/Python code) | ✗ No (just stores data) |
| **Custom code** | ✓ Written by developers | ✗ Third-party software |
| **Can be modified** | ✓ Change application code | ✗ Only configure |
| **Role** | Process requests, implement features | Store/cache data |

### Why This Matters for Fault Injection

From **Kubernetes perspective**: Both are just "workloads" - you can inject faults into either.

From **AIOps perspective**: The ROOT CAUSE location matters:
- Fault in `recommendation` pod → **Application-level** issue
- Fault in `mongodb-recommendation` pod → **Infrastructure/Storage** issue

**Example Scenario:**

```
User Request → frontend → recommendation → mongodb-recommendation
                              │                    │
                              │                    └── If MongoDB fails,
                              │                        recommendation can't
                              │                        fetch data
                              │
                              └── Agent should identify that the root cause
                                  is in mongodb-recommendation, not in
                                  recommendation service itself
```

### Viewing All "Services" in Kubernetes

```bash
# This shows ALL deployments (both microservices and backing services)
$ kubectl get deployments -n test-hotel-reservation

NAME                      READY   UP-TO-DATE   AVAILABLE
consul                    1/1     1            1          # Infrastructure
frontend                  1/1     1            1          # Microservice
geo                       1/1     1            1          # Microservice
memcached-profile         1/1     1            1          # Backing service
memcached-rate            1/1     1            1          # Backing service
mongodb-geo               1/1     1            1          # Backing service
mongodb-profile           1/1     1            1          # Backing service
mongodb-rate              1/1     1            1          # Backing service
mongodb-recommendation    1/1     1            1          # Backing service
profile                   1/1     1            1          # Microservice
rate                      1/1     1            1          # Microservice
recommendation            1/1     1            1          # Microservice
reservation               1/1     1            1          # Microservice
search                    1/1     1            1          # Microservice
user                      1/1     1            1          # Microservice
```

### Summary

| Term | Meaning |
|------|---------|
| **"Microservice"** | Architectural term - service with business logic |
| **"Backing Service"** | Architectural term - infrastructure (DB, cache) |
| **"Deployment"** | Kubernetes resource - manages pods |
| **"Service" (K8s)** | Kubernetes resource - network endpoint |
| **"Pod"** | Kubernetes resource - running container(s) |

**In Kubernetes, everything is a "workload"** - the microservice/backing-service distinction is for humans to understand the architecture, not for Kubernetes.

---

### Visual Example: Fault Injection Targeting

```
                    Inject "pod_failure" fault
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Namespace: test-hotel-reservation              │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ Deployment: recommendation                                  │ │
│  │ Labels: io.kompose.service=recommendation                   │ │
│  │                                                             │ │
│  │   ┌─────────────────┐    ┌─────────────────┐               │ │
│  │   │ Pod: rec-abc12  │    │ Pod: rec-def34  │  ← One pod    │ │
│  │   │ Status: Running │    │ Status: Running │    selected   │ │
│  │   │      │          │    │                 │    for fault  │ │
│  │   │      ▼          │    │                 │               │ │
│  │   │  ┌─────────┐    │    │  ┌─────────┐    │               │ │
│  │   │  │Container│    │    │  │Container│    │               │ │
│  │   │  └─────────┘    │    │  └─────────┘    │               │ │
│  │   └─────────────────┘    └─────────────────┘               │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │ Service: recommendation (ClusterIP)                         │ │
│  │ Routes traffic to pods with matching labels                 │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

---

## What is Fault Injection?

**Fault injection** is a technique to deliberately introduce errors/failures into a system to test its resilience and the ability of AIOps agents to detect, diagnose, and fix issues.

### Purpose

| Goal | Description |
|------|-------------|
| **Test Agent Capabilities** | Evaluate if agents can detect and fix various fault types |
| **Realistic Scenarios** | Simulate real-world production incidents |
| **Controlled Experiments** | Reproducible problems with known solutions |
| **Benchmark Comparison** | Compare different agents on the same problems |

---

## Fault Injection Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                     Fault Injector Classes                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
│  │ SymptomFault     │  │ ApplicationFault │  │ Virtualiza-  │  │
│  │ Injector         │  │ Injector         │  │ tionFault    │  │
│  │ (Chaos Mesh)     │  │ (App Layer)      │  │ Injector     │  │
│  └────────┬─────────┘  └────────┬─────────┘  └──────┬───────┘  │
│           │                     │                    │          │
│           └─────────────────────┼────────────────────┘          │
│                                 │                               │
│                    ┌────────────▼────────────┐                  │
│                    │    FaultInjector        │                  │
│                    │    (Base Class)         │                  │
│                    │  - _inject()            │                  │
│                    │  - _recover()           │                  │
│                    └────────────┬────────────┘                  │
│                                 │                               │
└─────────────────────────────────┼───────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Target System                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │ Kubernetes  │  │ Application │  │ Infrastructure          │  │
│  │ Resources   │  │ Components  │  │ (Network, Storage, OS)  │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Fault Injector Types

### 1. SymptomFaultInjector (Chaos Mesh)

Uses **Chaos Mesh** to inject infrastructure-level faults.

**File:** `aiopslab/generators/fault/inject_symp.py`

| Fault Type | Method | Description |
|------------|--------|-------------|
| `pod_failure` | `inject_pod_failure()` | Make pod unavailable |
| `pod_kill` | `inject_pod_kill()` | Kill pod (uses pod-failure) |
| `network_loss` | `inject_network_loss()` | 99% packet loss |
| `network_delay` | `inject_network_delay()` | Add network latency |
| `container_kill` | `inject_container_kill()` | Kill specific container |
| `kernel_fault` | `inject_kernel_fault()` | Kernel-level fault (experimental) |

**Example:**

```python
from aiopslab.generators.fault.inject_symp import SymptomFaultInjector

injector = SymptomFaultInjector(namespace="test-hotel-reservation")

# Inject pod failure
injector._inject(
    fault_type="pod_failure",
    microservices=["recommendation"],
    duration="100s"
)

# Recover
injector._recover(fault_type="pod_failure")
```

---

### 2. ApplicationFaultInjector

Injects faults at the application layer (MongoDB, code, config).

**File:** `aiopslab/generators/fault/inject_app.py`

| Fault Type | Method | Description |
|------------|--------|-------------|
| `revoke_auth` | `inject_revoke_auth()` | Revoke MongoDB admin privileges |
| `storage_user_unregistered` | `inject_storage_user_unregistered()` | Remove MongoDB user |
| `misconfig_app` | `inject_misconfig_app()` | Deploy buggy application image |
| `auth_miss_mongodb` | `inject_auth_miss_mongodb()` | Require TLS for MongoDB |

**Example:**

```python
from aiopslab.generators.fault.inject_app import ApplicationFaultInjector

injector = ApplicationFaultInjector(namespace="test-hotel-reservation")

# Inject auth revocation
injector._inject(
    fault_type="revoke_auth",
    microservices=["mongodb-geo"]
)

# Recover
injector._recover(
    fault_type="revoke_auth",
    microservices=["mongodb-geo"]
)
```

---

### 3. VirtualizationFaultInjector

Injects faults at the Kubernetes/Docker layer.

**File:** `aiopslab/generators/fault/inject_virtual.py`

| Fault Type | Method | Description |
|------------|--------|-------------|
| `misconfig_k8s` | `inject_misconfig_k8s()` | Wrong service target port |
| `scale_pods_to_zero` | `inject_scale_pods_to_zero()` | Scale deployment to 0 replicas |
| `assign_to_non_existent_node` | `inject_assign_to_non_existent_node()` | Schedule to non-existent node |
| `redeploy_without_pv` | `inject_redeploy_without_pv()` | Delete namespace, keep PV |
| `wrong_bin_usage` | `inject_wrong_bin_usage()` | Use wrong binary for service |
| `container_stop` | `inject_container_stop()` | Stop Docker container |
| `model_misconfig` | `inject_model_misconfig()` | ML model misconfiguration |

**Example:**

```python
from aiopslab.generators.fault.inject_virtual import VirtualizationFaultInjector

injector = VirtualizationFaultInjector(namespace="test-social-network")

# Scale pods to zero
injector._inject(
    fault_type="scale_pods_to_zero",
    microservices=["user-service"]
)

# Recover
injector._recover(
    fault_type="scale_pods_to_zero",
    microservices=["user-service"]
)
```

---

### 4. OSFaultInjector

Injects faults at the operating system layer using BPF.

**File:** `aiopslab/generators/fault/inject_os.py`

| Fault Type | Method | Description |
|------------|--------|-------------|
| `disk_woreout` | `inject_disk_woreout()` | Simulate disk I/O errors |

**Example:**

```python
from aiopslab.generators.fault.inject_os import OSFaultInjector

injector = OSFaultInjector()

# Inject disk failure
injector.inject_disk_woreout()

# Recover
injector.recover_disk_woreout()
```

---

### 5. K8SOperatorFaultInjector

Injects misconfigurations via Kubernetes operators (e.g., TiDB).

**File:** `aiopslab/generators/fault/inject_operator.py`

| Fault Type | Method | Description |
|------------|--------|-------------|
| `overload_replicas` | `inject_overload_replicas()` | Set 100000 replicas |
| `invalid_affinity_toleration` | `inject_invalid_affinity_toleration()` | Invalid toleration effect |
| `security_context_fault` | `inject_security_context_fault()` | Invalid runAsUser (-1) |
| `wrong_update_strategy` | `inject_wrong_update_strategy()` | Invalid update strategy |
| `non_existent_storage` | `inject_non_existent_storage()` | Non-existent storage class |

---

### 6. NoopFaultInjector

A no-operation injector for baseline testing.

**File:** `aiopslab/generators/fault/inject_noop.py`

```python
from aiopslab.generators.fault.inject_noop import NoopFaultInjector

injector = NoopFaultInjector(namespace="test-hotel-reservation")
injector._inject("no_op", ["geo"], "30s")  # Does nothing
injector._recover("no_op")  # Does nothing
```

---

## Fault Classification

### By System Level

| Level | Injector | Examples |
|-------|----------|----------|
| **Hardware** | OSFaultInjector | Disk wearout |
| **Operating System** | OSFaultInjector | Kernel faults |
| **Virtualization** | VirtualizationFaultInjector, SymptomFaultInjector | Pod failure, network issues |
| **Application** | ApplicationFaultInjector | Auth issues, misconfig |

### By Fault Type

| Type | Examples |
|------|----------|
| **Misconfiguration** | Wrong port, wrong binary, invalid strategy |
| **Code Defect** | Buggy application image |
| **Authentication Issue** | Revoked auth, missing TLS |
| **Network/Storage Issue** | Network loss/delay, disk wearout |
| **Operation Error** | Scale to zero, assign to non-existent node |
| **Dependency Problem** | Container kill, pod failure |

---

## Using Fault Injection in Problems

### Problem Definition Pattern

```python
from aiopslab.generators.fault.inject_symp import SymptomFaultInjector

class MyProblemBaseTask:
    def __init__(self):
        self.namespace = "test-hotel-reservation"
        self.faulty_service = "recommendation"
        self.injector = SymptomFaultInjector(namespace=self.namespace)

    def inject_fault(self):
        print("== Fault Injection ==")
        self.injector._inject(
            fault_type="pod_failure",
            microservices=[self.faulty_service],
            duration="100s",
        )

    def recover_fault(self):
        print("== Fault Recovery ==")
        self.injector._recover(fault_type="pod_failure")
```

### Complete Problem Example

```python
from aiopslab.orchestrator.tasks import MitigationTask
from aiopslab.generators.fault.inject_symp import SymptomFaultInjector
from aiopslab.service.apps.hotelres import HotelReservation

class NetworkDelayProblem(MitigationTask):
    def __init__(self):
        self.app = HotelReservation()
        self.namespace = self.app.namespace
        self.faulty_service = "geo"
        self.injector = SymptomFaultInjector(namespace=self.namespace)
        MitigationTask.__init__(self, self.app)

    def inject_fault(self):
        self.injector._inject(
            fault_type="network_delay",
            microservices=[self.faulty_service],
            duration="200s",
        )

    def recover_fault(self):
        self.injector._recover(fault_type="network_delay")

    def start_workload(self):
        # Start workload generator
        pass

    def eval(self, soln, trace, duration):
        # Evaluation logic
        pass
```

---

## Chaos Mesh Integration

SymptomFaultInjector uses **Chaos Mesh** under the hood.

### Chaos Mesh YAML Examples

#### Pod Failure

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: PodChaos
metadata:
  name: pod-failure-experiment
  namespace: test-hotel-reservation
spec:
  action: pod-failure
  mode: one
  duration: "100s"
  selector:
    labelSelectors:
      io.kompose.service: recommendation
```

#### Network Delay

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: delay
  namespace: test-hotel-reservation
spec:
  action: delay
  mode: one
  duration: "200s"
  selector:
    labelSelectors:
      io.kompose.service: geo
  delay:
    latency: "10s"
    correlation: "100"
    jitter: "0ms"
```

#### Network Loss

```yaml
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: loss
  namespace: test-hotel-reservation
spec:
  action: loss
  mode: one
  duration: "200s"
  selector:
    namespaces:
      - test-hotel-reservation
    labelSelectors:
      io.kompose.service: geo
  loss:
    loss: "99"
    correlation: "100"
```

---

## Creating Custom Faults

### Step 1: Choose Base Injector

```python
from aiopslab.generators.fault.base import FaultInjector

class MyCustomFaultInjector(FaultInjector):
    def __init__(self, namespace: str):
        super().__init__(namespace)
        self.namespace = namespace
```

### Step 2: Implement inject/recover Methods

```python
def inject_my_custom_fault(self, microservices: list[str], duration: str = "100s"):
    """Inject my custom fault."""
    for service in microservices:
        # Your fault injection logic here
        print(f"Injecting custom fault into {service}")

def recover_my_custom_fault(self, microservices: list[str] = None):
    """Recover from my custom fault."""
    # Your recovery logic here
    print("Recovering from custom fault")
```

### Step 3: Use in Problem Definition

```python
class MyCustomProblem:
    def __init__(self):
        self.injector = MyCustomFaultInjector(namespace="my-namespace")

    def inject_fault(self):
        self.injector._inject(
            fault_type="my_custom_fault",
            microservices=["service-a"],
            duration="60s"
        )

    def recover_fault(self):
        self.injector._recover(
            fault_type="my_custom_fault",
            microservices=["service-a"]
        )
```

---

## Fault Injection Flow

```
┌─────────────────┐
│ Problem.init()  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  app.deploy()   │  ← Deploy application
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ inject_fault()  │  ← Inject the fault
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│start_workload() │  ← Generate traffic
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Agent works... │  ← Agent tries to fix
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│    submit()     │  ← Agent submits solution
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│     eval()      │  ← Evaluate success
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ recover_fault() │  ← Clean up fault
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  app.cleanup()  │  ← Remove application
└─────────────────┘
```

---

## File Structure

```
aiopslab/generators/fault/
├── __init__.py
├── base.py              # FaultInjector base class
├── helpers.py           # Helper functions (get PIDs, process names)
├── inject_app.py        # ApplicationFaultInjector
├── inject_hw.py         # HardwareFaultInjector (placeholder)
├── inject_noop.py       # NoopFaultInjector
├── inject_operator.py   # K8SOperatorFaultInjector
├── inject_os.py         # OSFaultInjector
├── inject_otel.py       # OpenTelemetry-based injector
├── inject_symp.py       # SymptomFaultInjector (Chaos Mesh)
└── inject_virtual.py    # VirtualizationFaultInjector
```

---

## Summary: Available Faults

| Fault Type | Injector | System Level |
|------------|----------|--------------|
| pod_failure | SymptomFaultInjector | Virtualization |
| pod_kill | SymptomFaultInjector | Virtualization |
| network_loss | SymptomFaultInjector | Virtualization |
| network_delay | SymptomFaultInjector | Virtualization |
| container_kill | SymptomFaultInjector | Virtualization |
| kernel_fault | SymptomFaultInjector | OS |
| revoke_auth | ApplicationFaultInjector | Application |
| storage_user_unregistered | ApplicationFaultInjector | Application |
| misconfig_app | ApplicationFaultInjector | Application |
| auth_miss_mongodb | ApplicationFaultInjector | Application |
| misconfig_k8s | VirtualizationFaultInjector | Virtualization |
| scale_pods_to_zero | VirtualizationFaultInjector | Virtualization |
| assign_to_non_existent_node | VirtualizationFaultInjector | Virtualization |
| redeploy_without_pv | VirtualizationFaultInjector | Virtualization |
| wrong_bin_usage | VirtualizationFaultInjector | Virtualization |
| container_stop | VirtualizationFaultInjector | Virtualization |
| disk_woreout | OSFaultInjector | OS |
| overload_replicas | K8SOperatorFaultInjector | Virtualization |
| invalid_affinity_toleration | K8SOperatorFaultInjector | Virtualization |
| security_context_fault | K8SOperatorFaultInjector | Virtualization |
| wrong_update_strategy | K8SOperatorFaultInjector | Virtualization |
| non_existent_storage | K8SOperatorFaultInjector | Virtualization |
| no_op | NoopFaultInjector | None |
