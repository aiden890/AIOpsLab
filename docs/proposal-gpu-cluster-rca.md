# Proposal: GPU Cluster RCA System for AcmeTrace Kalos

## Executive Summary

This proposal outlines the design for a Root Cause Analysis (RCA) system for GPU clusters using the AcmeTrace Kalos dataset. The system will support three task types: **Detection**, **Localization**, and **Analysis** (no Mitigation).

---

## 1. Overview

### Goals

1. **Sample Dataset**: Create 1-week sample with failure jobs from Kalos cluster
2. **RCA Dataset**: Structure data for detection, localization, and analysis tasks
3. **AIOpsLab Integration**: Implement observer, actions, problems, and registry

### Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           GPU Cluster RCA System                             │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                      │
│  │  Detection  │ →  │Localization │ →  │  Analysis   │                      │
│  │             │    │             │    │             │                      │
│  │ "Is there   │    │ "Which GPU/ │    │ "What's the │                      │
│  │  a failure?"│    │  node failed│    │  root cause?│                      │
│  └─────────────┘    └─────────────┘    └─────────────┘                      │
│         │                  │                  │                              │
│         ▼                  ▼                  ▼                              │
│  ┌─────────────────────────────────────────────────────────────┐            │
│  │                    Kalos Dataset                             │            │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐     │            │
│  │  │Job Trace │  │GPU Metrics│  │XID Errors│  │Node Util │     │            │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘     │            │
│  └─────────────────────────────────────────────────────────────┘            │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Phase 1: Sample Dataset Creation

### 2.1 Sampling Strategy

Create a 1-week sample containing diverse failure scenarios:

```python
# Sampling criteria
SAMPLE_CONFIG = {
    "duration": "1 week",
    "target_jobs": 1000,
    "failure_ratio": 0.3,          # 30% failed jobs
    "include_states": ["FAILED", "COMPLETED", "CANCELLED"],
    "ensure_xid_errors": True,     # Include jobs with GPU errors
    "time_window": {
        "start": "2023-08-01",
        "end": "2023-08-07"
    }
}
```

### 2.2 Sample Dataset Structure

```
acme_cluster_dataset/
└── samples/
    └── kalos_1week/
        ├── job_trace/
        │   └── trace_kalos_sample.csv      # Sampled jobs (1000 jobs)
        ├── utilization/
        │   ├── GPU_UTIL.csv                # GPU utilization (filtered)
        │   ├── GPU_TEMP.csv                # GPU temperature (filtered)
        │   ├── XID_ERRORS.csv              # GPU errors (filtered)
        │   ├── NODE_CPU_UTILIZATION.csv    # Node CPU (filtered)
        │   └── NODE_MEMORY_UTILIZATION.csv # Node memory (filtered)
        ├── ground_truth/
        │   └── failure_labels.csv          # RCA ground truth
        └── queries/
            ├── detection_queries.csv       # Detection tasks
            ├── localization_queries.csv    # Localization tasks
            └── analysis_queries.csv        # Analysis tasks
```

### 2.3 Sampling Script

**File:** `acme_cluster_dataset/sample_kalos_rca.py`

```python
"""
Sample Kalos dataset for RCA tasks.

Usage:
    python sample_kalos_rca.py --duration 7 --num-jobs 1000 --failure-ratio 0.3
"""

def sample_jobs_with_failures(
    job_trace: pd.DataFrame,
    duration_days: int = 7,
    num_jobs: int = 1000,
    failure_ratio: float = 0.3
) -> pd.DataFrame:
    """
    Sample jobs ensuring:
    1. Time window of specified duration
    2. Target number of jobs
    3. Desired failure ratio
    4. Jobs with XID errors included
    """
    pass

def filter_utilization_by_jobs(
    utilization_df: pd.DataFrame,
    sampled_jobs: pd.DataFrame
) -> pd.DataFrame:
    """Filter utilization data to match sampled job time windows."""
    pass

def create_ground_truth(
    sampled_jobs: pd.DataFrame,
    xid_errors: pd.DataFrame
) -> pd.DataFrame:
    """
    Create ground truth labels:
    - failure_type: FAILED, TIMEOUT, NODE_FAIL
    - root_cause: XID_43, XID_31, RESOURCE_EXHAUSTION, UNKNOWN
    - affected_node: IP address
    - affected_gpu: GPU index
    """
    pass

def generate_queries(
    ground_truth: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Generate detection, localization, and analysis queries."""
    pass
```

---

## 3. Phase 2: RCA Dataset Design

### 3.1 Ground Truth Schema

**File:** `ground_truth/failure_labels.csv`

| Column | Type | Description |
|--------|------|-------------|
| `case_id` | int | Unique case identifier |
| `job_id` | string | Failed job ID |
| `failure_time` | datetime | When failure occurred |
| `failure_detected` | bool | True (ground truth for detection) |
| `affected_node` | string | Node IP (e.g., `172.31.15.112`) |
| `affected_gpu` | string | GPU ID (e.g., `172.31.15.112-6`) |
| `failure_type` | string | FAILED, TIMEOUT, NODE_FAIL, CANCELLED |
| `root_cause` | string | XID_43, XID_31, RESOURCE_EXHAUSTION, etc. |
| `root_cause_category` | string | GPU_ERROR, MEMORY, CPU, NETWORK, UNKNOWN |

### 3.2 Query Schemas

#### Detection Queries (`detection_queries.csv`)

| Column | Description |
|--------|-------------|
| `query_id` | Unique query ID |
| `case_id` | Reference to ground truth |
| `instruction` | "Analyze telemetry from {start_time} to {end_time}..." |
| `start_time` | Observation window start |
| `end_time` | Observation window end |
| `expected_answer` | "Yes" or "No" |

#### Localization Queries (`localization_queries.csv`)

| Column | Description |
|--------|-------------|
| `query_id` | Unique query ID |
| `case_id` | Reference to ground truth |
| `instruction` | "Identify which GPU/node failed..." |
| `start_time` | Observation window start |
| `end_time` | Observation window end |
| `expected_node` | Ground truth node |
| `expected_gpu` | Ground truth GPU |

#### Analysis Queries (`analysis_queries.csv`)

| Column | Description |
|--------|-------------|
| `query_id` | Unique query ID |
| `case_id` | Reference to ground truth |
| `instruction` | "Determine the root cause..." |
| `start_time` | Observation window start |
| `end_time` | Observation window end |
| `expected_root_cause` | Ground truth root cause |
| `expected_category` | Ground truth category |

### 3.3 Candidate Values

```python
# For agent to choose from
CANDIDATE_NODES = [
    "172.31.15.112", "172.31.15.118", "172.31.0.234", ...
]  # ~300 nodes

CANDIDATE_GPUS = [
    "172.31.15.112-0", "172.31.15.112-1", ..., "172.31.15.112-7",
    ...
]  # ~2400 GPUs

CANDIDATE_ROOT_CAUSES = [
    "XID_43 (GPU fell off bus)",
    "XID_31 (GPU memory ECC error)",
    "High GPU temperature",
    "GPU memory exhaustion",
    "CPU resource exhaustion",
    "Memory resource exhaustion",
    "Network timeout",
    "Job timeout",
    "User cancellation",
    "Unknown"
]

CANDIDATE_CATEGORIES = [
    "GPU_HARDWARE_ERROR",
    "GPU_MEMORY_ERROR",
    "THERMAL_THROTTLING",
    "RESOURCE_EXHAUSTION",
    "NETWORK_FAILURE",
    "TIMEOUT",
    "USER_ACTION",
    "UNKNOWN"
]
```

---

## 4. Phase 3: AIOpsLab Integration

### 4.1 File Structure

```
AIOpsLab/
├── aiopslab/
│   ├── observer/
│   │   └── acme_kalos.py                    # Dataset loader
│   ├── orchestrator/
│   │   ├── actions/
│   │   │   └── acme_kalos.py                # Action APIs
│   │   └── problems/
│   │       ├── acme/
│   │       │   ├── __init__.py
│   │       │   ├── base.py                  # Base problem class
│   │       │   ├── detection.py             # Detection problem
│   │       │   ├── localization.py          # Localization problem
│   │       │   └── analysis.py              # Analysis problem
│   │       └── acme_registry.py             # Problem registry
├── clients/
│   ├── run_acme_kalos.py                    # CLI entry point
│   └── utils/
│       └── acme_templates.py                # Prompt templates
└── acme_cluster_dataset/
    └── samples/
        └── kalos_1week/                     # Sampled dataset
```

### 4.2 Observer: Dataset Loader

**File:** `aiopslab/observer/acme_kalos.py`

```python
"""
Dataset loader for AcmeTrace Kalos GPU cluster data.
"""

import pandas as pd
from pathlib import Path
from datetime import datetime
from typing import Optional, Union

from aiopslab.paths import ACME_DATASET_DIR


class KalosDataset:
    """Provides access to Kalos GPU cluster telemetry."""

    def __init__(self, sample_name: str = "kalos_1week"):
        self.base_path = ACME_DATASET_DIR / "samples" / sample_name
        self._cache = {}

    # ==================== Job Data ====================

    def get_job_trace(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None,
        state: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Get job trace data with optional filtering.

        Returns:
            DataFrame with columns: job_id, user, node_num, gpu_num,
            cpu_num, state, submit_time, start_time, end_time,
            fail_time, duration, gpu_time
        """
        pass

    def get_failed_jobs(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None
    ) -> pd.DataFrame:
        """Get only failed jobs (state in FAILED, TIMEOUT, NODE_FAIL)."""
        pass

    # ==================== GPU Metrics ====================

    def get_gpu_utilization(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None,
        gpu_id: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Get GPU utilization metrics.

        Args:
            gpu_id: Filter by specific GPU (e.g., "172.31.15.112-6")

        Returns:
            DataFrame with columns: Time, {gpu_id_1}, {gpu_id_2}, ...
        """
        pass

    def get_gpu_temperature(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None,
        gpu_id: Optional[str] = None
    ) -> pd.DataFrame:
        """Get GPU temperature metrics."""
        pass

    def get_gpu_memory(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None,
        gpu_id: Optional[str] = None
    ) -> pd.DataFrame:
        """Get GPU memory usage (FB_USED)."""
        pass

    def get_gpu_power(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None,
        gpu_id: Optional[str] = None
    ) -> pd.DataFrame:
        """Get GPU power consumption."""
        pass

    # ==================== Error Data ====================

    def get_xid_errors(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None,
        gpu_id: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Get GPU XID error codes.

        Returns:
            DataFrame with columns: Time, {gpu_id_1}, {gpu_id_2}, ...
            Values are XID codes (0=no error, 31=memory error, 43=GPU off bus)
        """
        pass

    def get_xid_error_events(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None
    ) -> pd.DataFrame:
        """
        Get XID errors as events (non-zero values only).

        Returns:
            DataFrame with columns: timestamp, gpu_id, xid_code, xid_description
        """
        pass

    # ==================== Node Metrics ====================

    def get_node_cpu(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None,
        node_ip: Optional[str] = None
    ) -> pd.DataFrame:
        """Get node CPU utilization."""
        pass

    def get_node_memory(
        self,
        start_time: Optional[Union[str, int, datetime]] = None,
        end_time: Optional[Union[str, int, datetime]] = None,
        node_ip: Optional[str] = None
    ) -> pd.DataFrame:
        """Get node memory utilization."""
        pass

    # ==================== Ground Truth ====================

    def get_ground_truth(self) -> pd.DataFrame:
        """Get failure ground truth labels."""
        pass

    def get_queries(self, task_type: str) -> pd.DataFrame:
        """
        Get queries for a specific task type.

        Args:
            task_type: "detection", "localization", or "analysis"
        """
        pass

    # ==================== Metadata ====================

    def get_nodes(self) -> list[str]:
        """Get list of all node IPs."""
        pass

    def get_gpus(self) -> list[str]:
        """Get list of all GPU IDs."""
        pass

    def get_time_range(self) -> tuple[datetime, datetime]:
        """Get dataset time range (start, end)."""
        pass

    # ==================== Utilities ====================

    def _parse_time(
        self,
        time_val: Union[str, int, datetime, None]
    ) -> Optional[datetime]:
        """Parse various time formats to datetime."""
        pass

    def _filter_by_time(
        self,
        df: pd.DataFrame,
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        time_column: str = "Time"
    ) -> pd.DataFrame:
        """Filter DataFrame by time range."""
        pass
```

### 4.3 Actions: Agent APIs

**File:** `aiopslab/orchestrator/actions/acme_kalos.py`

```python
"""
Action APIs for AcmeTrace Kalos GPU cluster RCA.
"""

from aiopslab.utils.actions import action, read
from aiopslab.utils.status import SubmissionStatus
from aiopslab.observer.acme_kalos import KalosDataset


class KalosActions:
    """Provides telemetry APIs for GPU cluster RCA."""

    MAX_ROWS = 100  # Limit returned rows for agent readability

    def __init__(self, dataset: KalosDataset):
        self.dataset = dataset

    # ==================== Job APIs ====================

    @staticmethod
    @read
    def get_job_trace(
        start_time: str = None,
        end_time: str = None,
        state: str = None
    ) -> str:
        """
        Get job trace data for the specified time window.

        Args:
            start_time (str): Start time (ISO format or Unix timestamp)
            end_time (str): End time (ISO format or Unix timestamp)
            state (str): Filter by job state (COMPLETED, FAILED, CANCELLED)

        Returns:
            str: CSV-formatted job trace data
        """
        pass

    @staticmethod
    @read
    def get_failed_jobs(
        start_time: str = None,
        end_time: str = None
    ) -> str:
        """
        Get failed jobs (FAILED, TIMEOUT, NODE_FAIL) for the time window.

        Args:
            start_time (str): Start time
            end_time (str): End time

        Returns:
            str: CSV-formatted failed job data
        """
        pass

    # ==================== GPU Metrics APIs ====================

    @staticmethod
    @read
    def get_gpu_utilization(
        start_time: str = None,
        end_time: str = None,
        gpu_id: str = None
    ) -> str:
        """
        Get GPU utilization percentage (0-100).

        Args:
            start_time (str): Start time
            end_time (str): End time
            gpu_id (str): Specific GPU ID (e.g., "172.31.15.112-6")

        Returns:
            str: CSV-formatted GPU utilization data
        """
        pass

    @staticmethod
    @read
    def get_gpu_temperature(
        start_time: str = None,
        end_time: str = None,
        gpu_id: str = None
    ) -> str:
        """
        Get GPU temperature in Celsius.

        Args:
            start_time (str): Start time
            end_time (str): End time
            gpu_id (str): Specific GPU ID

        Returns:
            str: CSV-formatted GPU temperature data
        """
        pass

    @staticmethod
    @read
    def get_gpu_memory(
        start_time: str = None,
        end_time: str = None,
        gpu_id: str = None
    ) -> str:
        """
        Get GPU memory usage (frame buffer used in MB).

        Args:
            start_time (str): Start time
            end_time (str): End time
            gpu_id (str): Specific GPU ID

        Returns:
            str: CSV-formatted GPU memory data
        """
        pass

    # ==================== Error APIs ====================

    @staticmethod
    @read
    def get_xid_errors(
        start_time: str = None,
        end_time: str = None,
        gpu_id: str = None
    ) -> str:
        """
        Get GPU XID error codes.

        XID Codes:
        - 0: No error
        - 31: GPU memory page retirement / ECC error
        - 43: GPU has fallen off the bus (PCIe/NVLink failure)

        Args:
            start_time (str): Start time
            end_time (str): End time
            gpu_id (str): Specific GPU ID

        Returns:
            str: CSV-formatted XID error data
        """
        pass

    @staticmethod
    @read
    def get_xid_error_events(
        start_time: str = None,
        end_time: str = None
    ) -> str:
        """
        Get XID errors as events (only non-zero values).

        Returns:
            str: CSV with columns: timestamp, gpu_id, xid_code, description
        """
        pass

    # ==================== Node APIs ====================

    @staticmethod
    @read
    def get_node_cpu(
        start_time: str = None,
        end_time: str = None,
        node_ip: str = None
    ) -> str:
        """
        Get node CPU utilization percentage.

        Args:
            start_time (str): Start time
            end_time (str): End time
            node_ip (str): Specific node IP (e.g., "172.31.15.112")

        Returns:
            str: CSV-formatted CPU utilization data
        """
        pass

    @staticmethod
    @read
    def get_node_memory(
        start_time: str = None,
        end_time: str = None,
        node_ip: str = None
    ) -> str:
        """
        Get node memory utilization percentage.

        Args:
            start_time (str): Start time
            end_time (str): End time
            node_ip (str): Specific node IP

        Returns:
            str: CSV-formatted memory utilization data
        """
        pass

    # ==================== Metadata APIs ====================

    @staticmethod
    @read
    def get_node_list() -> str:
        """
        Get list of all node IPs in the cluster.

        Returns:
            str: Comma-separated list of node IPs
        """
        pass

    @staticmethod
    @read
    def get_gpu_list(node_ip: str = None) -> str:
        """
        Get list of all GPU IDs, optionally filtered by node.

        Args:
            node_ip (str): Filter GPUs by node IP

        Returns:
            str: Comma-separated list of GPU IDs
        """
        pass

    # ==================== Submit APIs ====================

    @staticmethod
    @action
    def submit_detection(has_failure: str) -> SubmissionStatus:
        """
        Submit detection result.

        Args:
            has_failure (str): "Yes" if failure detected, "No" otherwise

        Returns:
            SubmissionStatus: VALID_SUBMISSION or INVALID_SUBMISSION
        """
        return SubmissionStatus.VALID_SUBMISSION

    @staticmethod
    @action
    def submit_localization(
        node_ip: str,
        gpu_id: str = None
    ) -> SubmissionStatus:
        """
        Submit localization result.

        Args:
            node_ip (str): Affected node IP
            gpu_id (str): Affected GPU ID (optional)

        Returns:
            SubmissionStatus: VALID_SUBMISSION or INVALID_SUBMISSION
        """
        return SubmissionStatus.VALID_SUBMISSION

    @staticmethod
    @action
    def submit_analysis(
        root_cause: str,
        category: str
    ) -> SubmissionStatus:
        """
        Submit analysis result.

        Args:
            root_cause (str): Root cause description
            category (str): Root cause category

        Returns:
            SubmissionStatus: VALID_SUBMISSION or INVALID_SUBMISSION
        """
        return SubmissionStatus.VALID_SUBMISSION
```

### 4.4 Problems: Task Definitions

#### Base Problem Class

**File:** `aiopslab/orchestrator/problems/acme/base.py`

```python
"""
Base problem class for AcmeTrace Kalos GPU cluster RCA.
"""

from abc import ABC, abstractmethod
from aiopslab.orchestrator.tasks.base import Task
from aiopslab.observer.acme_kalos import KalosDataset
from aiopslab.orchestrator.actions.acme_kalos import KalosActions


class KalosBaseProblem(Task, ABC):
    """Base class for all Kalos RCA problems."""

    CANDIDATE_NODES = []  # Populated from dataset
    CANDIDATE_GPUS = []   # Populated from dataset

    CANDIDATE_ROOT_CAUSES = [
        "XID_43 (GPU fell off bus)",
        "XID_31 (GPU memory ECC error)",
        "High GPU temperature",
        "GPU memory exhaustion",
        "CPU resource exhaustion",
        "Memory resource exhaustion",
        "Network timeout",
        "Job timeout",
        "User cancellation",
        "Unknown"
    ]

    CANDIDATE_CATEGORIES = [
        "GPU_HARDWARE_ERROR",
        "GPU_MEMORY_ERROR",
        "THERMAL_THROTTLING",
        "RESOURCE_EXHAUSTION",
        "NETWORK_FAILURE",
        "TIMEOUT",
        "USER_ACTION",
        "UNKNOWN"
    ]

    def __init__(self, query_row: dict, sample_name: str = "kalos_1week"):
        super().__init__()
        self.query_id = query_row["query_id"]
        self.case_id = query_row["case_id"]
        self.instruction = query_row["instruction"]
        self.start_time = query_row["start_time"]
        self.end_time = query_row["end_time"]

        self.dataset = KalosDataset(sample_name=sample_name)
        self.actions = KalosActions(self.dataset)

        # Populate candidates from dataset
        self.CANDIDATE_NODES = self.dataset.get_nodes()
        self.CANDIDATE_GPUS = self.dataset.get_gpus()

        # Load ground truth for this case
        self.ground_truth = self._load_ground_truth(query_row)

    @abstractmethod
    def _load_ground_truth(self, query_row: dict) -> dict:
        """Load expected answer from query row."""
        pass

    @abstractmethod
    def get_task_description(self) -> str:
        """Return task-specific description."""
        pass

    def get_instructions(self) -> str:
        """Return the instruction for this specific query."""
        return self.instruction

    def get_available_actions(self) -> dict:
        """Return available APIs for the agent."""
        actions = {}
        for method_name in dir(self.actions):
            method = getattr(self.actions, method_name)
            if callable(method) and getattr(method, "is_action", False):
                actions[method_name] = method.__doc__.strip()
        return actions

    def perform_action(self, action_name: str, *args, **kwargs):
        """Execute an action by name."""
        if hasattr(self.actions, action_name):
            method = getattr(self.actions, action_name)
            return method(*args, **kwargs)
        raise ValueError(f"Unknown action: {action_name}")

    # No fault injection for static dataset
    def inject_fault(self):
        pass

    def recover_fault(self):
        pass

    def start_workload(self):
        pass
```

#### Detection Problem

**File:** `aiopslab/orchestrator/problems/acme/detection.py`

```python
"""
Detection task for Kalos GPU cluster RCA.
Task: Determine if there is a GPU failure in the given time window.
"""

from aiopslab.orchestrator.problems.acme.base import KalosBaseProblem


class KalosDetectionProblem(KalosBaseProblem):
    """Detection: Is there a GPU failure?"""

    TASK_TYPE = "detection"
    EVAL_FIELDS = ["detected"]

    def _load_ground_truth(self, query_row: dict) -> dict:
        return {
            "detected": query_row["expected_answer"]  # "Yes" or "No"
        }

    def get_task_description(self) -> str:
        return f"""
## GPU Cluster Failure Detection Task

You are an AI agent tasked with detecting GPU cluster failures.

**Time Window:** {self.start_time} to {self.end_time}

**Your Task:**
Analyze the telemetry data and determine if there is a GPU failure
(job failure, GPU error, or anomaly) in the specified time window.

**Available Data:**
- Job trace with failure states
- GPU metrics (utilization, temperature, memory)
- XID error codes (GPU hardware errors)
- Node metrics (CPU, memory)

**Submit your answer using:**
submit_detection(has_failure="Yes" or "No")
"""

    def eval(self, solution, trace: list, duration: float) -> dict:
        """Evaluate detection result."""
        self.common_eval(trace)

        # Normalize answer
        detected = str(solution).strip().lower()
        expected = self.ground_truth["detected"].strip().lower()

        correct = detected == expected

        self.add_result("TTD", duration)
        self.add_result("detected", detected)
        self.add_result("expected", expected)
        self.add_result("correct", correct)
        self.add_result("score", 1.0 if correct else 0.0)

        return self.results
```

#### Localization Problem

**File:** `aiopslab/orchestrator/problems/acme/localization.py`

```python
"""
Localization task for Kalos GPU cluster RCA.
Task: Identify which node/GPU is affected.
"""

from aiopslab.orchestrator.problems.acme.base import KalosBaseProblem


class KalosLocalizationProblem(KalosBaseProblem):
    """Localization: Which GPU/node failed?"""

    TASK_TYPE = "localization"
    EVAL_FIELDS = ["node", "gpu"]

    def _load_ground_truth(self, query_row: dict) -> dict:
        return {
            "node": query_row["expected_node"],
            "gpu": query_row.get("expected_gpu", None)
        }

    def get_task_description(self) -> str:
        return f"""
## GPU Cluster Failure Localization Task

You are an AI agent tasked with localizing GPU cluster failures.

**Time Window:** {self.start_time} to {self.end_time}

**Your Task:**
A failure has been detected in this time window. Identify which
node and GPU is affected.

**Available Nodes:** (sample)
{', '.join(self.CANDIDATE_NODES[:10])}... ({len(self.CANDIDATE_NODES)} total)

**Available GPUs:** (sample)
{', '.join(self.CANDIDATE_GPUS[:10])}... ({len(self.CANDIDATE_GPUS)} total)

**Submit your answer using:**
submit_localization(node_ip="...", gpu_id="...")
"""

    def eval(self, solution, trace: list, duration: float) -> dict:
        """Evaluate localization result."""
        self.common_eval(trace)

        # Parse solution
        if isinstance(solution, dict):
            pred_node = solution.get("node_ip", "")
            pred_gpu = solution.get("gpu_id", "")
        else:
            pred_node = str(solution)
            pred_gpu = ""

        # Compare
        node_correct = pred_node == self.ground_truth["node"]
        gpu_correct = (
            pred_gpu == self.ground_truth["gpu"]
            if self.ground_truth["gpu"] else True
        )

        score = (int(node_correct) + int(gpu_correct)) / 2.0

        self.add_result("TTL", duration)
        self.add_result("predicted_node", pred_node)
        self.add_result("predicted_gpu", pred_gpu)
        self.add_result("expected_node", self.ground_truth["node"])
        self.add_result("expected_gpu", self.ground_truth["gpu"])
        self.add_result("node_correct", node_correct)
        self.add_result("gpu_correct", gpu_correct)
        self.add_result("score", score)

        return self.results
```

#### Analysis Problem

**File:** `aiopslab/orchestrator/problems/acme/analysis.py`

```python
"""
Analysis task for Kalos GPU cluster RCA.
Task: Determine the root cause of the failure.
"""

from aiopslab.orchestrator.problems.acme.base import KalosBaseProblem


class KalosAnalysisProblem(KalosBaseProblem):
    """Analysis: What's the root cause?"""

    TASK_TYPE = "analysis"
    EVAL_FIELDS = ["root_cause", "category"]

    def _load_ground_truth(self, query_row: dict) -> dict:
        return {
            "root_cause": query_row["expected_root_cause"],
            "category": query_row["expected_category"]
        }

    def get_task_description(self) -> str:
        return f"""
## GPU Cluster Root Cause Analysis Task

You are an AI agent tasked with analyzing GPU cluster failures.

**Time Window:** {self.start_time} to {self.end_time}

**Your Task:**
A GPU failure has been detected and localized. Determine the root
cause of the failure.

**Possible Root Causes:**
{chr(10).join(f"- {rc}" for rc in self.CANDIDATE_ROOT_CAUSES)}

**Possible Categories:**
{chr(10).join(f"- {cat}" for cat in self.CANDIDATE_CATEGORIES)}

**XID Error Reference:**
- XID 31: GPU memory page retirement / ECC error
- XID 43: GPU has fallen off the bus (PCIe/NVLink failure)

**Submit your answer using:**
submit_analysis(root_cause="...", category="...")
"""

    def eval(self, solution, trace: list, duration: float) -> dict:
        """Evaluate analysis result."""
        self.common_eval(trace)

        # Parse solution
        if isinstance(solution, dict):
            pred_cause = solution.get("root_cause", "")
            pred_category = solution.get("category", "")
        else:
            pred_cause = str(solution)
            pred_category = ""

        # Normalize for comparison
        def normalize(s):
            return s.lower().strip().replace("_", " ")

        cause_correct = normalize(pred_cause) == normalize(
            self.ground_truth["root_cause"]
        )
        category_correct = normalize(pred_category) == normalize(
            self.ground_truth["category"]
        )

        score = (int(cause_correct) + int(category_correct)) / 2.0

        self.add_result("TTA", duration)
        self.add_result("predicted_root_cause", pred_cause)
        self.add_result("predicted_category", pred_category)
        self.add_result("expected_root_cause", self.ground_truth["root_cause"])
        self.add_result("expected_category", self.ground_truth["category"])
        self.add_result("cause_correct", cause_correct)
        self.add_result("category_correct", category_correct)
        self.add_result("score", score)

        return self.results
```

### 4.5 Registry: Problem ID Mapping

**File:** `aiopslab/orchestrator/problems/acme_registry.py`

```python
"""
Registry for AcmeTrace Kalos GPU cluster RCA problems.
"""

import pandas as pd
from pathlib import Path

from aiopslab.paths import ACME_DATASET_DIR
from aiopslab.orchestrator.problems.acme.detection import KalosDetectionProblem
from aiopslab.orchestrator.problems.acme.localization import KalosLocalizationProblem
from aiopslab.orchestrator.problems.acme.analysis import KalosAnalysisProblem


class AcmeProblemRegistry:
    """
    Registry for Acme Kalos GPU cluster RCA problems.

    Problem ID Format: "acme-kalos-{task_type}-{query_id}"
    Examples:
        - acme-kalos-detection-0
        - acme-kalos-localization-5
        - acme-kalos-analysis-10
    """

    SUPPORTED_TASKS = ["detection", "localization", "analysis"]

    TASK_CLASS_MAP = {
        "detection": KalosDetectionProblem,
        "localization": KalosLocalizationProblem,
        "analysis": KalosAnalysisProblem,
    }

    def __init__(self, sample_name: str = "kalos_1week"):
        self.sample_name = sample_name
        self.base_path = ACME_DATASET_DIR / "samples" / sample_name
        self._queries_cache = {}

    def _load_queries(self, task_type: str) -> pd.DataFrame:
        """Load queries for a task type."""
        if task_type not in self._queries_cache:
            query_file = self.base_path / "queries" / f"{task_type}_queries.csv"
            if query_file.exists():
                self._queries_cache[task_type] = pd.read_csv(query_file)
            else:
                raise FileNotFoundError(f"Query file not found: {query_file}")
        return self._queries_cache[task_type]

    def get_problem_instance(self, problem_id: str):
        """
        Get a problem instance by ID.

        Args:
            problem_id: Format "acme-kalos-{task_type}-{query_id}"

        Returns:
            Problem instance (Detection, Localization, or Analysis)
        """
        parts = problem_id.split("-")
        if len(parts) != 4 or parts[0] != "acme" or parts[1] != "kalos":
            raise ValueError(f"Invalid problem ID format: {problem_id}")

        task_type = parts[2]
        query_id = int(parts[3])

        if task_type not in self.SUPPORTED_TASKS:
            raise ValueError(f"Unknown task type: {task_type}")

        queries = self._load_queries(task_type)
        query_row = queries[queries["query_id"] == query_id].iloc[0].to_dict()

        problem_class = self.TASK_CLASS_MAP[task_type]
        return problem_class(query_row, sample_name=self.sample_name)

    def get_problem_ids(
        self,
        task_type: str = None
    ) -> list[str]:
        """
        Get all available problem IDs.

        Args:
            task_type: Filter by task type (optional)

        Returns:
            List of problem IDs
        """
        problem_ids = []

        tasks = [task_type] if task_type else self.SUPPORTED_TASKS

        for task in tasks:
            try:
                queries = self._load_queries(task)
                for query_id in queries["query_id"]:
                    problem_ids.append(f"acme-kalos-{task}-{query_id}")
            except FileNotFoundError:
                continue

        return problem_ids

    def get_problem_deployment(self, problem_id: str) -> str:
        """Return deployment type (always 'static' for Acme dataset)."""
        return "static"
```

### 4.6 Client: CLI Entry Point

**File:** `clients/run_acme_kalos.py`

```python
#!/usr/bin/env python3
"""
CLI for running AcmeTrace Kalos GPU cluster RCA tasks.

Usage:
    # List available problems
    python run_acme_kalos.py --list
    python run_acme_kalos.py --list --task detection

    # Run single problem
    python run_acme_kalos.py --task detection --query-id 0

    # Run batch
    python run_acme_kalos.py --task analysis --batch --limit 10

    # Run all tasks
    python run_acme_kalos.py --batch --limit 50
"""

import argparse
import asyncio
import json
from pathlib import Path

from aiopslab.orchestrator.static_orchestrator import StaticDatasetOrchestrator
from aiopslab.orchestrator.problems.acme_registry import AcmeProblemRegistry
from clients.utils.acme_templates import ACME_SYSTEM_PROMPT, ACME_RESP_INSTR
from clients.utils.llm import LLMAgent


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run AcmeTrace Kalos GPU cluster RCA tasks"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List available problem IDs"
    )
    parser.add_argument(
        "--task", type=str,
        choices=["detection", "localization", "analysis"],
        help="Task type to run"
    )
    parser.add_argument(
        "--query-id", type=int,
        help="Specific query ID to run"
    )
    parser.add_argument(
        "--batch", action="store_true",
        help="Run batch of problems"
    )
    parser.add_argument(
        "--limit", type=int, default=10,
        help="Limit number of problems in batch mode"
    )
    parser.add_argument(
        "--output", type=str,
        help="Output file for results (JSON)"
    )
    parser.add_argument(
        "--max-steps", type=int, default=15,
        help="Maximum steps per problem"
    )
    parser.add_argument(
        "--sample", type=str, default="kalos_1week",
        help="Sample dataset name"
    )
    return parser.parse_args()


class AcmeKalosAgent(LLMAgent):
    """Agent for Acme Kalos RCA tasks."""

    def init_context(self, problem_desc, instructions, apis):
        self.system_message = ACME_SYSTEM_PROMPT.format(
            problem_desc=problem_desc,
            apis=self._format_apis(apis)
        )
        self.task_message = instructions
        self.resp_instr = ACME_RESP_INSTR

    def _format_apis(self, apis: dict) -> str:
        return "\n".join(
            f"- {name}: {doc}" for name, doc in apis.items()
        )


async def run_single_problem(
    problem_id: str,
    max_steps: int = 15
) -> dict:
    """Run a single problem and return results."""
    orchestrator = StaticDatasetOrchestrator()
    agent = AcmeKalosAgent()

    orchestrator.register_agent(agent, name="acme-kalos-agent")

    task_desc, instructions, actions = orchestrator.init_problem(problem_id)
    agent.init_context(task_desc, instructions, actions)

    results = await orchestrator.start_problem(max_steps=max_steps)

    return {
        "problem_id": problem_id,
        "results": results["results"],
        "steps": len(results["history"]),
    }


async def main():
    args = parse_args()
    registry = AcmeProblemRegistry(sample_name=args.sample)

    if args.list:
        problem_ids = registry.get_problem_ids(task_type=args.task)
        print(f"Available problems ({len(problem_ids)}):")
        for pid in problem_ids:
            print(f"  {pid}")
        return

    if args.query_id is not None and args.task:
        # Single problem
        problem_id = f"acme-kalos-{args.task}-{args.query_id}"
        results = await run_single_problem(problem_id, args.max_steps)
        print(json.dumps(results, indent=2))

    elif args.batch:
        # Batch mode
        problem_ids = registry.get_problem_ids(task_type=args.task)
        problem_ids = problem_ids[:args.limit]

        all_results = []
        for i, pid in enumerate(problem_ids):
            print(f"[{i+1}/{len(problem_ids)}] Running {pid}...")
            result = await run_single_problem(pid, args.max_steps)
            all_results.append(result)
            print(f"  Score: {result['results'].get('score', 'N/A')}")

        if args.output:
            with open(args.output, "w") as f:
                json.dump(all_results, f, indent=2)
            print(f"Results saved to {args.output}")

        # Summary
        scores = [r["results"].get("score", 0) for r in all_results]
        print(f"\nSummary: {sum(scores)/len(scores):.2%} accuracy")

    else:
        print("Specify --task and --query-id, or use --batch mode")


if __name__ == "__main__":
    asyncio.run(main())
```

### 4.7 Templates: Prompt Templates

**File:** `clients/utils/acme_templates.py`

```python
"""
Prompt templates for AcmeTrace Kalos GPU cluster RCA.
"""

ACME_SYSTEM_PROMPT = """{problem_desc}

## Available APIs

You can use the following APIs to analyze the GPU cluster telemetry:

{apis}

## Important Notes

1. Time format: Use ISO format (e.g., "2023-08-01T12:00:00") or Unix timestamp
2. GPU ID format: "IP-GPU_INDEX" (e.g., "172.31.15.112-6")
3. Node IP format: "172.31.x.x"
4. XID Codes:
   - 0: No error
   - 31: GPU memory ECC error
   - 43: GPU fell off bus (critical hardware failure)

## Response Format

At each turn, respond with:

Thought: <your reasoning about what to investigate next>
Action: <API call to execute>

When ready to submit, use the appropriate submit function.
"""

ACME_RESP_INSTR = """Based on the previous output, continue your investigation.

DO NOT repeat the same API calls. Analyze the data and either:
1. Call a different API to gather more information
2. Submit your final answer if you have enough evidence

Respond with:
Thought: <your analysis of the previous output>
Action: <your next action>
"""
```

---

## 5. Implementation Timeline

| Phase | Task | Duration |
|-------|------|----------|
| **Phase 1** | Sample Dataset Creation | 1-2 days |
| | - Implement `sample_kalos_rca.py` | |
| | - Generate ground truth labels | |
| | - Create query files | |
| **Phase 2** | Observer Implementation | 1 day |
| | - Implement `acme_kalos.py` dataset loader | |
| | - Add time filtering and caching | |
| **Phase 3** | Actions Implementation | 1 day |
| | - Implement all action APIs | |
| | - Add data truncation for agents | |
| **Phase 4** | Problems Implementation | 2 days |
| | - Base problem class | |
| | - Detection, Localization, Analysis tasks | |
| | - Evaluation logic | |
| **Phase 5** | Registry & Client | 1 day |
| | - Problem registry | |
| | - CLI entry point | |
| | - Prompt templates | |
| **Phase 6** | Testing & Documentation | 1-2 days |
| | - Unit tests | |
| | - Integration tests | |
| | - Documentation | |

**Total: ~7-9 days**

---

## 6. Next Steps

1. **Approve this proposal** - Confirm the design meets requirements
2. **Start Phase 1** - Create sampling script and generate dataset
3. **Iterate** - Refine based on initial results

Would you like me to proceed with implementation?
