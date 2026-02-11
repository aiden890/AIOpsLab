# Static Log Replayer Proposal

**Status**: Draft
**Author**: System Design
**Date**: 2026-02-10
**Target Dataset**: OpenRCA (initial), expandable to Alibaba, ACME

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Motivation](#motivation)
3. [Architecture Overview](#architecture-overview)
4. [Component Design](#component-design)
5. [Directory Structure](#directory-structure)
6. [Data Flow](#data-flow)
7. [Configuration Design](#configuration-design)
8. [Docker Lifecycle Management](#docker-lifecycle-management)
9. [Logging & Debugging Design](#logging--debugging-design)
10. [Results File Format](#results-file-format)
11. [Implementation Plan](#implementation-plan)
12. [Expandability Strategy](#expandability-strategy)
13. [Open Questions](#open-questions)

---

## Executive Summary

The Static Log Replayer system enables testing AIOps agents against historical datasets (OpenRCA, Alibaba, ACME) by simulating real-world telemetry generation without requiring live Kubernetes clusters or fault injection infrastructure.

**Key Design Decisions**:
- **Push-based architecture**: Replayer Docker writes to CSV files, agents read from them
- **Standalone Docker**: Replayer runs outside K8s for simplicity
- **Smart cleanup**: Stop container + clear volume data (keep image for reuse)
- **Dataset-organized problems**: Static problems grouped by dataset (OpenRCA, Alibaba, ACME)
- **Separate orchestrator**: New `StaticOrchestrator` class independent of live orchestrator
- **New agent actions**: Dataset-agnostic static query actions
- **Expandable design**: Plugin architecture for adding new datasets
- **Developer-friendly**: Comprehensive logging, structured results, easy debugging

---

## Motivation

### Current Limitations

AIOpsLab currently supports only **live fault injection** scenarios:
```
Agent → Orchestrator → Deploy K8s App → Inject Fault → Observe Live Telemetry
```

This approach has limitations:
- ❌ Requires live K8s cluster and observability stack (Prometheus, Jaeger, Elasticsearch)
- ❌ Can't replay historical incidents from research datasets
- ❌ Difficult to reproduce exact same conditions
- ❌ Time-consuming setup and teardown

### Proposed Solution

Static Log Replayer enables **historical dataset replay**:
```
Agent → Static Orchestrator → Start Replayer Docker → Query Replayed Telemetry
```

Benefits:
- ✅ Test against real-world incidents (OpenRCA, Alibaba, ACME datasets)
- ✅ Reproducible experiments (same data every time)
- ✅ Faster iteration (no K8s deployment overhead)
- ✅ Multi-dataset support (expandable architecture)
- ✅ Isolated testing (agents can't access raw datasets)

---

## Architecture Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                  Static Log Replayer System                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ Dataset (Read-Only)                                     │    │
│  │  openrca_dataset/Bank/                                  │    │
│  │    ├─ query.csv         (RCA tasks)                     │    │
│  │    ├─ record.csv        (ground truth)                  │    │
│  │    └─ telemetry/YYYY_MM_DD/                             │    │
│  │         ├─ log/log_service.csv                          │    │
│  │         ├─ trace/trace_span.csv                         │    │
│  │         └─ metric/metric_{app,container}.csv            │    │
│  └────────────────────────────────────────────────────────┘    │
│                              │                                   │
│                              ▼ (mounted read-only)               │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ Replayer Docker (Standalone)                            │    │
│  │                                                         │    │
│  │  [Phase 1: Bulk History Loading]                       │    │
│  │    • Load all telemetry before simulation start_time   │    │
│  │    • Remap timestamps: historical → current            │    │
│  │    • Write to shared volume CSV files                  │    │
│  │                                                         │    │
│  │  [Phase 2: Real-Time Streaming]                        │    │
│  │    • Stream telemetry with timestamp offset            │    │
│  │    • Append to CSV files (simulating live data)        │    │
│  │    • Respect configured telemetry types                │    │
│  └────────────────────────────────────────────────────────┘    │
│                              │                                   │
│                              ▼ (writes)                          │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ Shared Volume: /telemetry_output/                       │    │
│  │    ├─ trace.csv      (continuously growing)             │    │
│  │    ├─ log.csv                                           │    │
│  │    └─ metric.csv                                        │    │
│  └────────────────────────────────────────────────────────┘    │
│                              │                                   │
│                              ▼ (reads)                           │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ Static Observer                                         │    │
│  │  aiopslab/observer/static/openrca/                      │    │
│  │    ├─ OpenRCATraceAPI                                   │    │
│  │    ├─ OpenRCALogAPI                                     │    │
│  │    └─ OpenRCAMetricAPI                                  │    │
│  │                                                         │    │
│  │  • Read CSV files                                       │    │
│  │  • Filter by time window                                │    │
│  │  • Convert to standard format                           │    │
│  └────────────────────────────────────────────────────────┘    │
│                              │                                   │
│                              ▼                                   │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ Static Actions                                          │    │
│  │  aiopslab/orchestrator/actions/static_actions.py        │    │
│  │    • query_static_traces(start, end, filters)           │    │
│  │    • query_static_logs(start, end, filters)             │    │
│  │    • query_static_metrics(start, end, filters)          │    │
│  └────────────────────────────────────────────────────────┘    │
│                              │                                   │
│                              ▼                                   │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ Agent (existing clients)                                │    │
│  │    • Uses new static actions                            │    │
│  │    • Dataset-agnostic queries                           │    │
│  │    • Same agent code for all datasets                   │    │
│  └────────────────────────────────────────────────────────┘    │
│                              │                                   │
│                              ▼                                   │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ Static Orchestrator                                     │    │
│  │  aiopslab/orchestrator/static_orchestrator.py           │    │
│  │    • Manages replayer Docker lifecycle                  │    │
│  │    • Loads static problems                              │    │
│  │    • Coordinates simulation timing                      │    │
│  └────────────────────────────────────────────────────────┘    │
│                              │                                   │
│                              ▼                                   │
│  ┌────────────────────────────────────────────────────────┐    │
│  │ Static Problem                                          │    │
│  │  aiopslab/orchestrator/static_problems/openrca/         │    │
│  │    ├─ problems.py       (task definitions)              │    │
│  │    ├─ evaluator.py      (dataset-specific eval)         │    │
│  │    └─ loader.py         (load query.csv, record.csv)    │    │
│  │                                                         │    │
│  │  • Define RCA tasks                                     │    │
│  │  • Load ground truth                                    │    │
│  │  • Evaluate solutions (dataset-specific criteria)       │    │
│  └────────────────────────────────────────────────────────┘    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Push Method Rationale

**Why Push (Replayer → CSV) vs Pull (Agent → Replayer API)?**

| Aspect | Push Method ✅ | Pull Method |
|--------|---------------|-------------|
| Complexity | Simple: just write CSV | Complex: HTTP server, connection pooling |
| Debugging | Easy: inspect CSV files | Hard: ephemeral responses |
| History Support | Natural: pre-write history, then stream | Complex: need to track what was requested |
| Real-world Match | Yes: telemetry systems write data | No: backward from reality |
| Agent Isolation | Yes: agents read replayed data only | Yes: API controls access |
| Statefulness | Stateless: CSV files persist | Stateful: replayer tracks requests |

**Decision**: Push method for simplicity, debuggability, and natural history support.

---

## Component Design

### 1. Replayer Docker

**Location**: `aiopslab-applications/static-replayers/openrca/`

**Files**:
```
openrca/
├── Dockerfile
├── replayer.py
├── time_mapper.py
├── config_schema.yaml
├── requirements.txt
└── README.md
```

**Responsibilities**:
1. Load dataset from mounted volume (read-only)
2. Load configuration (time mapping, telemetry types)
3. **Phase 1 - Bulk History**: Write all telemetry before `start_time` to CSV
4. **Phase 2 - Streaming**: Stream telemetry in real-time with offset
5. Handle timestamp remapping (historical → current simulation time)

**Configuration Example**:
```yaml
# Mounted at /config/replayer_config.yaml
dataset:
  type: openrca
  namespace: Bank
  path: /datasets/openrca_dataset/Bank

simulation:
  start_time_option: fault_time
  offset_minutes: -5  # Start 5 min before fault

time_mapping:
  historical_fault_time: "2021-03-04 14:57:00"
  simulation_start_time: "2026-02-10 10:00:00"  # Auto-calculated or specified

telemetry:
  enabled:
    - trace
    - log
    - metric
  history_window_minutes: 60  # Load 60 min of history before start

output:
  path: /telemetry_output
  format: csv
```

**Timestamp Remapping Logic**:
```python
# time_mapper.py
class TimeMapper:
    def __init__(self, historical_fault_time, simulation_start_time, offset_minutes):
        self.offset = calculate_offset(historical_fault_time, simulation_start_time, offset_minutes)

    def remap(self, historical_timestamp):
        """Convert historical timestamp to simulation timestamp"""
        return historical_timestamp + self.offset
```

**Replayer Lifecycle**:
```python
# replayer.py pseudo-code
def main():
    config = load_config()
    dataset = load_dataset(config.dataset.path)
    time_mapper = TimeMapper(config.time_mapping)

    # Phase 1: Bulk load history
    print("Loading historical telemetry...")
    history_telemetry = dataset.get_telemetry_before(
        config.simulation.start_time,
        window_minutes=config.telemetry.history_window_minutes
    )
    write_to_csv(history_telemetry, time_mapper, config.output.path)

    # Phase 2: Stream real-time data
    print("Starting real-time streaming...")
    for timestamp in dataset.get_telemetry_stream(config.simulation.start_time):
        remapped_data = time_mapper.remap(timestamp)
        append_to_csv(remapped_data, config.output.path)
        sleep_until_next_timestamp()
```

**Docker Compose Setup**:
```yaml
# docker-compose.yml (for testing)
version: '3.8'
services:
  openrca-replayer:
    build: ./aiopslab-applications/static-replayers/openrca
    volumes:
      - ./openrca_dataset:/datasets/openrca_dataset:ro
      - ./telemetry_output:/telemetry_output
      - ./configs/openrca_bank.yaml:/config/replayer_config.yaml:ro
    environment:
      - LOG_LEVEL=INFO
```

---

### 2. Static Application Manager

**Location**: `aiopslab/service/apps/static_replayer/`

**Files**:
```
static_replayer/
├── __init__.py
├── base.py           # BaseStaticApp
├── openrca.py        # OpenRCAStaticApp
├── alibaba.py        # AlibabaStaticApp (future)
├── acme.py           # ACMEStaticApp (future)
└── configs/
    ├── openrca_bank.yaml
    ├── openrca_telecom.yaml
    ├── openrca_market_cloudbed1.yaml
    └── ...
```

**Base Class**:
```python
# base.py
from abc import ABC, abstractmethod
import docker
from pathlib import Path

class BaseStaticApp(ABC):
    """Base class for static log replayer applications."""

    def __init__(self, config_file: str):
        self.config_file = config_file
        self.config = self.load_config()
        self.docker_client = docker.from_env()
        self.container = None

    @abstractmethod
    def load_config(self) -> dict:
        """Load application configuration."""
        pass

    @abstractmethod
    def get_dataset_path(self) -> Path:
        """Return path to dataset."""
        pass

    def start_replayer(self):
        """Start the replayer Docker container."""
        print(f"Starting replayer for {self.config['dataset']['namespace']}...")

        self.container = self.docker_client.containers.run(
            image=self.get_docker_image(),
            volumes=self.get_volumes(),
            environment=self.get_environment(),
            detach=True,
            remove=True
        )

        print(f"Replayer started: {self.container.id}")

    def stop_replayer(self):
        """Stop the replayer Docker container."""
        if self.container:
            self.container.stop()
            print("Replayer stopped.")

    def get_volumes(self) -> dict:
        """Get volume mappings for Docker."""
        return {
            str(self.get_dataset_path()): {
                'bind': '/datasets',
                'mode': 'ro'
            },
            str(self.get_output_path()): {
                'bind': '/telemetry_output',
                'mode': 'rw'
            },
            str(self.config_file): {
                'bind': '/config/replayer_config.yaml',
                'mode': 'ro'
            }
        }

    @abstractmethod
    def get_docker_image(self) -> str:
        """Return Docker image name."""
        pass
```

**OpenRCA Implementation**:
```python
# openrca.py
from aiopslab.service.apps.static_replayer.base import BaseStaticApp
from aiopslab.paths import BASE_DIR
from pathlib import Path
import yaml

class OpenRCAStaticApp(BaseStaticApp):
    """OpenRCA static log replayer application."""

    def load_config(self) -> dict:
        with open(self.config_file, 'r') as f:
            return yaml.safe_load(f)

    def get_dataset_path(self) -> Path:
        # Default: openrca_dataset/{namespace}
        namespace = self.config['dataset']['namespace']
        return BASE_DIR / f"openrca_dataset/{namespace}"

    def get_output_path(self) -> Path:
        # Default: aiopslab/data/telemetry_output/{namespace}
        namespace = self.config['dataset']['namespace']
        output_dir = BASE_DIR / f"aiopslab/data/telemetry_output/{namespace}"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    def get_docker_image(self) -> str:
        return "aiopslab-static-replayer-openrca:latest"

    def get_telemetry_apis(self):
        """Get static observer APIs for this dataset."""
        from aiopslab.observer.static.openrca import (
            OpenRCATraceAPI,
            OpenRCALogAPI,
            OpenRCAMetricAPI
        )

        output_path = self.get_output_path()
        return {
            'trace': OpenRCATraceAPI(output_path),
            'log': OpenRCALogAPI(output_path),
            'metric': OpenRCAMetricAPI(output_path)
        }
```

**Config Example** (`configs/openrca_bank.yaml`):
```yaml
dataset:
  type: openrca
  namespace: Bank
  # path auto-resolved by OpenRCAStaticApp

simulation:
  start_time_option: fault_time  # Options: fault_time, fault_minus_Xmin, query_start, absolute
  offset_minutes: -5  # Start simulation 5 min before fault

time_mapping:
  # These will be populated by the problem definition
  historical_fault_time: null  # Set by problem
  simulation_start_time: null  # Auto-calculated

telemetry:
  enabled:
    - trace
    - log
    - metric
  history_window_minutes: 60

output:
  path: /telemetry_output  # Inside Docker
  format: csv
```

---

### 3. Static Observer

**Location**: `aiopslab/observer/static/`

**Files**:
```
static/
├── __init__.py
├── base.py           # Base classes
├── openrca/
│   ├── __init__.py
│   ├── trace_api.py
│   ├── log_api.py
│   └── metric_api.py
├── alibaba/
│   └── ...
└── acme/
    └── ...
```

**Base Class**:
```python
# base.py
from abc import ABC, abstractmethod
from pathlib import Path
from datetime import datetime
import pandas as pd

class StaticObserverBase(ABC):
    """Base class for static dataset observers."""

    def __init__(self, output_path: Path):
        self.output_path = Path(output_path)

    @abstractmethod
    def extract_data(self, start_time: datetime, end_time: datetime, **filters):
        """Extract data within time window with optional filters."""
        pass

    @abstractmethod
    def get_csv_path(self) -> Path:
        """Return path to CSV file."""
        pass

    def _read_csv_with_time_filter(self, start_time: datetime, end_time: datetime) -> pd.DataFrame:
        """Read CSV and filter by time window."""
        csv_path = self.get_csv_path()

        if not csv_path.exists():
            return pd.DataFrame()

        df = pd.read_csv(csv_path)

        # Convert timestamp column to datetime
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')

        # Filter by time window
        mask = (df['datetime'] >= start_time) & (df['datetime'] <= end_time)
        return df[mask]
```

**OpenRCA Trace API**:
```python
# openrca/trace_api.py
from aiopslab.observer.static.base import StaticObserverBase
from pathlib import Path
from datetime import datetime
import pandas as pd

class OpenRCATraceAPI(StaticObserverBase):
    """Static trace API for OpenRCA dataset."""

    def get_csv_path(self) -> Path:
        return self.output_path / "trace.csv"

    def extract_data(self, start_time: datetime, end_time: datetime, **filters):
        """
        Extract trace spans within time window.

        Args:
            start_time: Start of time window
            end_time: End of time window
            filters: Optional filters (cmdb_id, trace_id, etc.)

        Returns:
            pd.DataFrame with columns: timestamp, cmdb_id, parent_id, span_id, trace_id, duration
        """
        df = self._read_csv_with_time_filter(start_time, end_time)

        # Apply additional filters
        if 'cmdb_id' in filters:
            df = df[df['cmdb_id'] == filters['cmdb_id']]

        if 'trace_id' in filters:
            df = df[df['trace_id'] == filters['trace_id']]

        return df

    def get_trace_by_id(self, trace_id: str):
        """Get all spans for a specific trace."""
        csv_path = self.get_csv_path()
        if not csv_path.exists():
            return pd.DataFrame()

        df = pd.read_csv(csv_path)
        return df[df['trace_id'] == trace_id]

    def get_traces_by_service(self, cmdb_id: str, start_time: datetime, end_time: datetime):
        """Get all traces for a specific service."""
        return self.extract_data(start_time, end_time, cmdb_id=cmdb_id)
```

**OpenRCA Log API**:
```python
# openrca/log_api.py
from aiopslab.observer.static.base import StaticObserverBase
from pathlib import Path
from datetime import datetime
import pandas as pd

class OpenRCALogAPI(StaticObserverBase):
    """Static log API for OpenRCA dataset."""

    def get_csv_path(self) -> Path:
        return self.output_path / "log.csv"

    def extract_data(self, start_time: datetime, end_time: datetime, **filters):
        """
        Extract logs within time window.

        Args:
            start_time: Start of time window
            end_time: End of time window
            filters: Optional filters (cmdb_id, log_name, keyword, etc.)

        Returns:
            pd.DataFrame with columns: log_id, timestamp, cmdb_id, log_name, value
        """
        df = self._read_csv_with_time_filter(start_time, end_time)

        # Apply filters
        if 'cmdb_id' in filters:
            df = df[df['cmdb_id'] == filters['cmdb_id']]

        if 'log_name' in filters:
            df = df[df['log_name'] == filters['log_name']]

        if 'keyword' in filters:
            df = df[df['value'].str.contains(filters['keyword'], case=False, na=False)]

        return df

    def search_logs(self, keyword: str, start_time: datetime, end_time: datetime):
        """Search logs for keyword."""
        return self.extract_data(start_time, end_time, keyword=keyword)
```

**OpenRCA Metric API**:
```python
# openrca/metric_api.py
from aiopslab.observer.static.base import StaticObserverBase
from pathlib import Path
from datetime import datetime
import pandas as pd

class OpenRCAMetricAPI(StaticObserverBase):
    """Static metric API for OpenRCA dataset."""

    def get_csv_path(self) -> Path:
        return self.output_path / "metric.csv"

    def extract_data(self, start_time: datetime, end_time: datetime, **filters):
        """
        Extract metrics within time window.

        Args:
            start_time: Start of time window
            end_time: End of time window
            filters: Optional filters (cmdb_id, metric_name, etc.)

        Returns:
            pd.DataFrame with metrics
        """
        df = self._read_csv_with_time_filter(start_time, end_time)

        # Apply filters
        if 'cmdb_id' in filters:
            df = df[df['cmdb_id'] == filters['cmdb_id']]

        if 'metric_name' in filters:
            df = df[df['metric_name'] == filters['metric_name']]

        return df

    def get_metric_timeseries(self, cmdb_id: str, metric_name: str,
                               start_time: datetime, end_time: datetime):
        """Get time series for a specific metric."""
        return self.extract_data(
            start_time, end_time,
            cmdb_id=cmdb_id,
            metric_name=metric_name
        )
```

---

### 4. Static Actions

**Location**: `aiopslab/orchestrator/actions/static_actions.py`

**Purpose**: Dataset-agnostic action interface for agents

```python
# static_actions.py
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import pandas as pd

class StaticActions:
    """
    Dataset-agnostic actions for querying static replayed telemetry.

    These actions work across all datasets (OpenRCA, Alibaba, ACME) by using
    a common interface to the dataset-specific observer APIs.
    """

    def __init__(self, observer_apis: Dict[str, Any]):
        """
        Initialize with dataset-specific observer APIs.

        Args:
            observer_apis: Dict with keys 'trace', 'log', 'metric' mapping to
                          dataset-specific API instances
        """
        self.trace_api = observer_apis.get('trace')
        self.log_api = observer_apis.get('log')
        self.metric_api = observer_apis.get('metric')

    def query_static_traces(self, start_time: datetime, end_time: datetime,
                           **filters) -> pd.DataFrame:
        """
        Query trace spans within time window.

        Args:
            start_time: Start of query window
            end_time: End of query window
            **filters: Dataset-specific filters (cmdb_id, trace_id, etc.)

        Returns:
            DataFrame with trace data

        Example:
            traces = query_static_traces(
                start_time=datetime(2026, 2, 10, 10, 0),
                end_time=datetime(2026, 2, 10, 10, 30),
                cmdb_id='Tomcat01'
            )
        """
        if not self.trace_api:
            raise ValueError("Trace API not available for this dataset")

        return self.trace_api.extract_data(start_time, end_time, **filters)

    def query_static_logs(self, start_time: datetime, end_time: datetime,
                         **filters) -> pd.DataFrame:
        """
        Query logs within time window.

        Args:
            start_time: Start of query window
            end_time: End of query window
            **filters: Dataset-specific filters (cmdb_id, keyword, log_name, etc.)

        Returns:
            DataFrame with log data

        Example:
            logs = query_static_logs(
                start_time=datetime(2026, 2, 10, 10, 0),
                end_time=datetime(2026, 2, 10, 10, 30),
                cmdb_id='Redis02',
                keyword='OutOfMemory'
            )
        """
        if not self.log_api:
            raise ValueError("Log API not available for this dataset")

        return self.log_api.extract_data(start_time, end_time, **filters)

    def query_static_metrics(self, start_time: datetime, end_time: datetime,
                            **filters) -> pd.DataFrame:
        """
        Query metrics within time window.

        Args:
            start_time: Start of query window
            end_time: End of query window
            **filters: Dataset-specific filters (cmdb_id, metric_name, etc.)

        Returns:
            DataFrame with metric data

        Example:
            metrics = query_static_metrics(
                start_time=datetime(2026, 2, 10, 10, 0),
                end_time=datetime(2026, 2, 10, 10, 30),
                cmdb_id='Mysql02',
                metric_name='memory_usage'
            )
        """
        if not self.metric_api:
            raise ValueError("Metric API not available for this dataset")

        return self.metric_api.extract_data(start_time, end_time, **filters)

    def get_available_services(self) -> List[str]:
        """
        Get list of available services (cmdb_ids) in the dataset.

        Returns:
            List of service identifiers
        """
        # Read from trace CSV (usually has all services)
        if self.trace_api:
            csv_path = self.trace_api.get_csv_path()
            if csv_path.exists():
                df = pd.read_csv(csv_path)
                return df['cmdb_id'].unique().tolist()

        return []

    def search_anomalies(self, start_time: datetime, end_time: datetime,
                        threshold: float = 3.0) -> Dict[str, Any]:
        """
        Search for anomalies in metrics using simple statistical methods.

        Args:
            start_time: Start of search window
            end_time: End of search window
            threshold: Number of standard deviations for anomaly detection

        Returns:
            Dict with anomalous services and metrics
        """
        # TODO: Implement basic anomaly detection
        # This is a helper action to assist agents
        pass
```

---

### 5. Static Orchestrator

**Location**: `aiopslab/orchestrator/static_orchestrator.py`

**Purpose**: Manage lifecycle of static problem solving (separate from live orchestrator)

```python
# static_orchestrator.py
"""Static Orchestrator for historical dataset replay."""

from aiopslab.session import Session
from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry
from aiopslab.orchestrator.parser import ResponseParser
from aiopslab.utils.status import *
import time
import atexit

class StaticOrchestrator:
    """
    Orchestrator for static log replayer scenarios.

    Unlike the live orchestrator, this does not:
    - Deploy K8s applications
    - Inject faults
    - Manage Prometheus/Jaeger/Elasticsearch

    Instead, it:
    - Starts replayer Docker containers
    - Manages simulation timing
    - Coordinates static problems
    """

    def __init__(self, results_dir=None):
        self.agent = None
        self.session = None
        self.parser = ResponseParser()
        self.probs = StaticProblemRegistry()
        self.sprint = SessionPrint()
        self.execution_start_time = None
        self.execution_end_time = None
        self.results_dir = results_dir
        self.current_problem = None

    def init_static_problem(self, problem_id: str):
        """
        Initialize a static problem for the agent to solve.

        Args:
            problem_id: Problem identifier (e.g., 'openrca_bank_task1')

        Returns:
            tuple: (task_desc, instructions, actions)
        """
        # Start timer
        self.execution_start_time = time.time()

        # Create session
        self.session = Session(results_dir=self.results_dir)
        print(f"Session ID: {self.session.session_id}")

        # Get problem instance
        prob = self.probs.get_problem_instance(problem_id)
        self.current_problem = prob
        self.session.set_problem(prob, pid=problem_id)
        self.session.set_agent(self.agent_name)

        # Start replayer (instead of deploying K8s app)
        print("Starting static log replayer...")
        prob.start_replayer()
        atexit.register(self._cleanup_replayer, prob=prob)

        # Get task information
        task_desc = prob.get_task_description()
        instructions = prob.get_instructions()
        actions = prob.get_available_actions()

        return task_desc, instructions, actions

    def _cleanup_replayer(self, prob):
        """Stop the replayer Docker container on exit."""
        try:
            prob.stop_replayer()
        except Exception as e:
            print(f"Error stopping replayer: {e}")

    def register_agent(self, agent, name="agent"):
        """Register the agent for the current session."""
        self.agent = agent
        self.agent_name = name

    def step(self, agent_response: str):
        """
        Execute one step of agent interaction.

        Args:
            agent_response: Agent's response containing action calls

        Returns:
            Environment response
        """
        # Parse agent response
        action_name, args, kwargs = self.parser.parse_response(agent_response)

        # Execute action
        result = self.current_problem.perform_action(action_name, *args, **kwargs)

        # Record in session
        self.session.add_agent_message(agent_response)
        self.session.add_env_message(str(result))

        return result

    def submit_solution(self, solution: Any) -> Dict[str, Any]:
        """
        Submit final solution for evaluation.

        Args:
            solution: Agent's solution (format depends on task type)

        Returns:
            Evaluation results
        """
        self.execution_end_time = time.time()
        duration = self.execution_end_time - self.execution_start_time

        # Evaluate solution
        trace = self.session.get_trace()
        results = self.current_problem.eval(solution, trace, duration)

        # Save session
        self.session.save()

        return results
```

---

### 6. Static Problems

**Location**: `aiopslab/orchestrator/static_problems/`

**Files**:
```
static_problems/
├── __init__.py
├── base.py               # BaseStaticProblem
├── registry.py           # StaticProblemRegistry
├── openrca/
│   ├── __init__.py
│   ├── problems.py       # OpenRCA problem definitions
│   ├── evaluator.py      # OpenRCA-specific evaluation
│   └── loader.py         # Load query.csv, record.csv
├── alibaba/
│   └── ...
└── acme/
    └── ...
```

**Base Class**:
```python
# base.py
from abc import ABC, abstractmethod
from typing import Any, Dict, List
from datetime import datetime
from aiopslab.session import SessionItem
from aiopslab.orchestrator.actions.static_actions import StaticActions

class BaseStaticProblem(ABC):
    """Base class for static dataset problems."""

    def __init__(self):
        self.app = None  # Static replayer app
        self.actions = None  # StaticActions instance
        self.results = {}
        self.ground_truth = None

    @abstractmethod
    def load_ground_truth(self):
        """Load ground truth from dataset (e.g., record.csv)."""
        pass

    @abstractmethod
    def get_task_description(self) -> str:
        """Return task description for the agent."""
        pass

    @abstractmethod
    def get_instructions(self) -> str:
        """Return detailed instructions for the agent."""
        pass

    def get_available_actions(self) -> List[str]:
        """Return list of available action names."""
        return [
            'query_static_traces',
            'query_static_logs',
            'query_static_metrics',
            'get_available_services',
            'submit_solution'
        ]

    def start_replayer(self):
        """Start the replayer Docker container."""
        print(f"Starting replayer for {self.app.config['dataset']['namespace']}...")
        self.app.start_replayer()

        # Wait for replayer to finish bulk history loading
        self._wait_for_replayer_ready()

        # Initialize static actions with observer APIs
        observer_apis = self.app.get_telemetry_apis()
        self.actions = StaticActions(observer_apis)

        print("Replayer ready. Telemetry data available.")

    def stop_replayer(self):
        """Stop the replayer Docker container."""
        self.app.stop_replayer()

    def _wait_for_replayer_ready(self, timeout=300):
        """Wait for replayer to finish loading history."""
        # Check if output CSVs exist and have data
        import time
        start = time.time()
        while time.time() - start < timeout:
            if self._check_telemetry_ready():
                return
            time.sleep(5)
        raise TimeoutError("Replayer did not become ready in time")

    def _check_telemetry_ready(self) -> bool:
        """Check if telemetry CSV files are ready."""
        output_path = self.app.get_output_path()
        trace_csv = output_path / "trace.csv"
        # Check if file exists and has content
        return trace_csv.exists() and trace_csv.stat().st_size > 0

    def perform_action(self, action_name: str, *args, **kwargs) -> Any:
        """Execute an action."""
        if not hasattr(self.actions, action_name):
            raise ValueError(f"Unknown action: {action_name}")

        action_func = getattr(self.actions, action_name)
        return action_func(*args, **kwargs)

    @abstractmethod
    def eval(self, soln: Any, trace: List[SessionItem], duration: float) -> Dict[str, Any]:
        """
        Evaluate the solution.

        Args:
            soln: Agent's solution
            trace: Session trace
            duration: Execution duration

        Returns:
            Evaluation results
        """
        pass

    def add_result(self, key: str, value: Any):
        """Add evaluation result."""
        self.results[key] = value
```

**OpenRCA Problems**:
```python
# openrca/problems.py
from aiopslab.orchestrator.static_problems.base import BaseStaticProblem
from aiopslab.orchestrator.static_problems.openrca.loader import OpenRCALoader
from aiopslab.orchestrator.static_problems.openrca.evaluator import OpenRCAEvaluator
from aiopslab.service.apps.static_replayer.openrca import OpenRCAStaticApp
from aiopslab.paths import BASE_DIR
from datetime import datetime, timedelta

class OpenRCABankTask1(BaseStaticProblem):
    """
    OpenRCA Bank - Task 1: Root Cause Time Detection

    Task: Identify the specific occurrence time of the root cause.
    Scoring: Within 1 minute of actual root cause time.
    """

    def __init__(self):
        super().__init__()

        # Initialize app
        config_file = BASE_DIR / "aiopslab/service/apps/static_replayer/configs/openrca_bank_task1.yaml"
        self.app = OpenRCAStaticApp(str(config_file))

        # Load dataset metadata
        self.loader = OpenRCALoader(
            dataset_path=BASE_DIR / "openrca_dataset/Bank"
        )

        # Load this specific task
        self.task_info = self.loader.get_task_by_index("task_1", task_number=0)
        self.ground_truth = self.loader.get_ground_truth_for_task(self.task_info)

        # Evaluator
        self.evaluator = OpenRCAEvaluator()

    def load_ground_truth(self):
        """Ground truth already loaded in __init__."""
        return self.ground_truth

    def get_task_description(self) -> str:
        """Return task description from query.csv."""
        return f"""
# Root Cause Analysis Task

**Dataset**: OpenRCA Bank
**Task Type**: Root Cause Time Detection

{self.task_info['instruction']}

**Your Goal**:
Analyze the telemetry data (traces, logs, metrics) and identify the exact
timestamp when the root cause occurred.

**Time Window**:
- Query Start: {self.task_info['query_start_time']}
- Query End: {self.task_info['query_end_time']}

**Available Actions**:
- query_static_traces(start_time, end_time, **filters)
- query_static_logs(start_time, end_time, **filters)
- query_static_metrics(start_time, end_time, **filters)
- get_available_services()

**Submission Format**:
Submit your answer as a datetime object:
```python
submit_solution(datetime(2026, 2, 10, 10, 57, 0))
```
"""

    def get_instructions(self) -> str:
        """Return detailed instructions."""
        return """
You are tasked with performing Root Cause Analysis on a historical incident
from the OpenRCA Bank dataset.

**Step-by-step approach**:

1. **Explore the services**: Use `get_available_services()` to see what
   services are in the system.

2. **Query telemetry data**: Use the query actions to retrieve traces, logs,
   and metrics within the given time window.

3. **Analyze patterns**: Look for anomalies, errors, performance degradation,
   or other indicators of failure.

4. **Identify root cause time**: Determine the exact timestamp when the root
   cause first occurred.

5. **Submit solution**: Use `submit_solution(datetime(...))` to submit your
   answer.

**Important Notes**:
- The ground truth root cause time is known from historical records
- Your solution will be evaluated based on how close it is to the actual time
- You must be within 1 minute to get full credit

**Dataset-specific hints**:
- OpenRCA failures typically involve: high memory usage, network latency,
  network packet loss
- Services include: Tomcat, Redis, MySQL, Apache, MG, IG
- Look for sudden changes in metrics or error logs
"""

    def eval(self, soln: Any, trace: List[SessionItem], duration: float) -> Dict[str, Any]:
        """
        Evaluate the solution.

        Args:
            soln: Datetime object representing predicted root cause time
            trace: Session trace
            duration: Execution duration

        Returns:
            Evaluation results
        """
        # Use OpenRCA-specific evaluator
        results = self.evaluator.evaluate_time_detection(
            predicted_time=soln,
            ground_truth_time=self.ground_truth['timestamp'],
            threshold_minutes=1
        )

        self.results.update(results)
        self.results['duration'] = duration

        return self.results


class OpenRCABankTask6(BaseStaticProblem):
    """
    OpenRCA Bank - Task 6: Root Cause Component and Reason Detection

    Task: Identify both the root cause component and reason.
    """

    def __init__(self):
        super().__init__()

        config_file = BASE_DIR / "aiopslab/service/apps/static_replayer/configs/openrca_bank_task6.yaml"
        self.app = OpenRCAStaticApp(str(config_file))

        self.loader = OpenRCALoader(
            dataset_path=BASE_DIR / "openrca_dataset/Bank"
        )

        self.task_info = self.loader.get_task_by_index("task_6", task_number=0)
        self.ground_truth = self.loader.get_ground_truth_for_task(self.task_info)

        self.evaluator = OpenRCAEvaluator()

    def load_ground_truth(self):
        return self.ground_truth

    def get_task_description(self) -> str:
        return f"""
# Root Cause Analysis Task

**Dataset**: OpenRCA Bank
**Task Type**: Root Cause Component and Reason Detection

{self.task_info['instruction']}

**Your Goal**:
Identify BOTH:
1. The root cause component (service that failed)
2. The root cause reason (why it failed)

**Submission Format**:
```python
submit_solution({{
    'component': 'Redis02',
    'reason': 'high memory usage'
}})
```
"""

    def get_instructions(self) -> str:
        return """
Root Cause Component and Reason Analysis

**Approach**:
1. Query telemetry data across all services
2. Identify which service(s) show anomalous behavior
3. Determine the root cause (component with earliest anomaly)
4. Classify the reason based on telemetry patterns:
   - High memory usage: Look for OOM errors, memory metrics spiking
   - Network latency: Look for increased request durations
   - Network packet loss: Look for connection errors, retries

**Possible Root Cause Reasons** (OpenRCA dataset):
- high memory usage
- network latency
- network packet loss

Submit both component and reason as a dictionary.
"""

    def eval(self, soln: Any, trace: List[SessionItem], duration: float) -> Dict[str, Any]:
        """Evaluate component and reason prediction."""
        results = self.evaluator.evaluate_component_and_reason(
            predicted_component=soln.get('component'),
            predicted_reason=soln.get('reason'),
            ground_truth_component=self.ground_truth['component'],
            ground_truth_reason=self.ground_truth['reason']
        )

        self.results.update(results)
        self.results['duration'] = duration

        return self.results
```

**OpenRCA Loader**:
```python
# openrca/loader.py
"""Loader for OpenRCA dataset query and record files."""

import pandas as pd
from pathlib import Path
from datetime import datetime

class OpenRCALoader:
    """Load OpenRCA dataset metadata (query.csv, record.csv)."""

    def __init__(self, dataset_path: Path):
        self.dataset_path = Path(dataset_path)
        self.query_df = pd.read_csv(self.dataset_path / "query.csv")
        self.record_df = pd.read_csv(self.dataset_path / "record.csv")

    def get_task_by_index(self, task_type: str, task_number: int) -> dict:
        """
        Get task information by task type and index.

        Args:
            task_type: Task type (e.g., 'task_1', 'task_6')
            task_number: Index of task (0-based)

        Returns:
            Dict with task information
        """
        tasks = self.query_df[self.query_df['task_index'] == task_type]

        if task_number >= len(tasks):
            raise ValueError(f"Task {task_type} index {task_number} not found")

        task_row = tasks.iloc[task_number]

        # Parse instruction to extract time window
        # Example: "On March 4, 2021, within the time range of 14:30 to 15:00..."
        # This parsing logic would be dataset-specific

        return {
            'task_index': task_row['task_index'],
            'instruction': task_row['instruction'],
            'scoring_points': task_row['scoring_points'],
            # These would be parsed from instruction or separate columns
            'query_start_time': None,  # TODO: Parse from instruction
            'query_end_time': None,
        }

    def get_ground_truth_for_task(self, task_info: dict) -> dict:
        """
        Get ground truth for a task.

        This requires matching task info with record.csv based on time window.
        """
        # TODO: Implement matching logic
        # For now, return first record as example
        record_row = self.record_df.iloc[0]

        return {
            'level': record_row['level'],
            'component': record_row['component'],
            'timestamp': datetime.fromtimestamp(record_row['timestamp']),
            'datetime': record_row['datetime'],
            'reason': record_row['reason']
        }

    def get_all_tasks(self) -> pd.DataFrame:
        """Return all tasks."""
        return self.query_df

    def get_all_ground_truths(self) -> pd.DataFrame:
        """Return all ground truth records."""
        return self.record_df
```

**OpenRCA Evaluator**:
```python
# openrca/evaluator.py
"""OpenRCA-specific evaluation logic."""

from datetime import datetime, timedelta
from typing import Dict, Any

class OpenRCAEvaluator:
    """Evaluator for OpenRCA dataset problems."""

    def evaluate_time_detection(self, predicted_time: datetime,
                                ground_truth_time: datetime,
                                threshold_minutes: int = 1) -> Dict[str, Any]:
        """
        Evaluate root cause time detection.

        Scoring: Within threshold_minutes of ground truth = success

        Args:
            predicted_time: Agent's predicted time
            ground_truth_time: Actual root cause time
            threshold_minutes: Allowed error margin (default: 1 minute)

        Returns:
            Evaluation results
        """
        if predicted_time is None:
            return {
                'time_accuracy': 'Invalid',
                'time_error_minutes': float('inf'),
                'success': False
            }

        # Calculate time difference
        time_diff = abs((predicted_time - ground_truth_time).total_seconds() / 60)

        # Check if within threshold
        success = time_diff <= threshold_minutes

        return {
            'time_accuracy': 'Correct' if success else 'Incorrect',
            'time_error_minutes': time_diff,
            'success': success,
            'predicted_time': predicted_time.isoformat(),
            'ground_truth_time': ground_truth_time.isoformat()
        }

    def evaluate_component_detection(self, predicted_component: str,
                                     ground_truth_component: str) -> Dict[str, Any]:
        """Evaluate root cause component detection."""
        if predicted_component is None:
            return {
                'component_accuracy': 'Invalid',
                'success': False
            }

        success = predicted_component.lower() == ground_truth_component.lower()

        return {
            'component_accuracy': 'Correct' if success else 'Incorrect',
            'success': success,
            'predicted_component': predicted_component,
            'ground_truth_component': ground_truth_component
        }

    def evaluate_reason_detection(self, predicted_reason: str,
                                  ground_truth_reason: str) -> Dict[str, Any]:
        """Evaluate root cause reason detection."""
        if predicted_reason is None:
            return {
                'reason_accuracy': 'Invalid',
                'success': False
            }

        # Normalize reasons for comparison
        predicted_norm = predicted_reason.lower().strip()
        ground_truth_norm = ground_truth_reason.lower().strip()

        success = predicted_norm == ground_truth_norm

        return {
            'reason_accuracy': 'Correct' if success else 'Incorrect',
            'success': success,
            'predicted_reason': predicted_reason,
            'ground_truth_reason': ground_truth_reason
        }

    def evaluate_component_and_reason(self, predicted_component: str,
                                     predicted_reason: str,
                                     ground_truth_component: str,
                                     ground_truth_reason: str) -> Dict[str, Any]:
        """Evaluate both component and reason together."""
        component_results = self.evaluate_component_detection(
            predicted_component, ground_truth_component
        )
        reason_results = self.evaluate_reason_detection(
            predicted_reason, ground_truth_reason
        )

        # Success only if both are correct
        combined_success = component_results['success'] and reason_results['success']

        return {
            **component_results,
            **reason_results,
            'combined_success': combined_success
        }
```

**Static Problem Registry**:
```python
# registry.py
"""Registry for static problems."""

from typing import Dict, Type
from aiopslab.orchestrator.static_problems.base import BaseStaticProblem

class StaticProblemRegistry:
    """Registry for static dataset problems."""

    def __init__(self):
        self._problems: Dict[str, Type[BaseStaticProblem]] = {}
        self._register_all()

    def _register_all(self):
        """Register all available problems."""
        # OpenRCA problems
        from aiopslab.orchestrator.static_problems.openrca.problems import (
            OpenRCABankTask1,
            OpenRCABankTask6,
            # Add more as implemented
        )

        self.register('openrca_bank_task1', OpenRCABankTask1)
        self.register('openrca_bank_task6', OpenRCABankTask6)

        # Future: Alibaba, ACME problems
        # self.register('alibaba_cluster_task1', AlibabaClusterTask1)

    def register(self, problem_id: str, problem_class: Type[BaseStaticProblem]):
        """Register a problem class."""
        self._problems[problem_id] = problem_class

    def get_problem_instance(self, problem_id: str) -> BaseStaticProblem:
        """Get an instance of a problem."""
        if problem_id not in self._problems:
            raise ValueError(f"Unknown problem: {problem_id}")

        return self._problems[problem_id]()

    def list_problems(self) -> list:
        """List all registered problems."""
        return list(self._problems.keys())
```

---

### 7. Client for Static RCA

**Location**: `clients/static_rca_client.py`

**Purpose**: Example client for running static RCA experiments

```python
# static_rca_client.py
"""Client for running static RCA experiments."""

from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator
from clients.gpt import GPTClient  # Or any agent client
import argparse

def run_static_rca(problem_id: str, agent_type: str = 'gpt'):
    """
    Run a static RCA experiment.

    Args:
        problem_id: Problem identifier (e.g., 'openrca_bank_task1')
        agent_type: Agent type ('gpt', 'claude', etc.)
    """
    # Initialize orchestrator
    orchestrator = StaticOrchestrator(results_dir='./results/static_rca')

    # Initialize agent
    if agent_type == 'gpt':
        agent = GPTClient()
    else:
        raise ValueError(f"Unknown agent type: {agent_type}")

    orchestrator.register_agent(agent, name=agent_type)

    # Initialize problem
    task_desc, instructions, actions = orchestrator.init_static_problem(problem_id)

    # Run agent
    print("=" * 80)
    print("TASK DESCRIPTION")
    print("=" * 80)
    print(task_desc)
    print("\n")
    print("=" * 80)
    print("INSTRUCTIONS")
    print("=" * 80)
    print(instructions)
    print("\n")

    # Agent interaction loop
    agent_response = agent.run(task_desc, instructions, actions)

    # For simplicity, assume agent returns final solution
    # In reality, this would be a multi-step interaction loop

    # Submit solution
    # Parse solution from agent response
    solution = parse_solution(agent_response)  # TODO: Implement parsing

    results = orchestrator.submit_solution(solution)

    print("=" * 80)
    print("EVALUATION RESULTS")
    print("=" * 80)
    print(results)

    return results


def parse_solution(agent_response: str):
    """Parse solution from agent response."""
    # TODO: Implement solution parsing based on response format
    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run static RCA experiment')
    parser.add_argument('--problem', type=str, required=True,
                       help='Problem ID (e.g., openrca_bank_task1)')
    parser.add_argument('--agent', type=str, default='gpt',
                       help='Agent type (gpt, claude, etc.)')

    args = parser.parse_args()

    run_static_rca(args.problem, args.agent)
```

---

## Directory Structure

Complete directory structure:

```
AIOpsLab/
├── aiopslab/
│   ├── observer/
│   │   ├── static/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── openrca/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── trace_api.py
│   │   │   │   ├── log_api.py
│   │   │   │   └── metric_api.py
│   │   │   ├── alibaba/
│   │   │   │   └── ...
│   │   │   └── acme/
│   │   │       └── ...
│   │   └── (existing live observer files)
│   │
│   ├── service/
│   │   ├── apps/
│   │   │   ├── static_replayer/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── base.py
│   │   │   │   ├── openrca.py
│   │   │   │   ├── alibaba.py
│   │   │   │   ├── acme.py
│   │   │   │   └── configs/
│   │   │   │       ├── openrca_bank_task1.yaml
│   │   │   │       ├── openrca_bank_task6.yaml
│   │   │   │       ├── openrca_telecom.yaml
│   │   │   │       └── ...
│   │   │   └── (existing apps)
│   │   └── ...
│   │
│   ├── orchestrator/
│   │   ├── static_orchestrator.py        # NEW
│   │   ├── orchestrator.py               # Existing
│   │   ├── static_problems/              # NEW
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── registry.py
│   │   │   ├── openrca/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── problems.py
│   │   │   │   ├── evaluator.py
│   │   │   │   └── loader.py
│   │   │   ├── alibaba/
│   │   │   │   └── ...
│   │   │   └── acme/
│   │   │       └── ...
│   │   ├── problems/                     # Existing live problems
│   │   ├── actions/
│   │   │   ├── static_actions.py         # NEW
│   │   │   └── (existing actions)
│   │   └── ...
│   │
│   └── data/
│       └── telemetry_output/             # NEW: Shared volume for replayed data
│           ├── Bank/
│           ├── Telecom/
│           └── ...
│
├── aiopslab-applications/
│   └── static-replayers/
│       ├── openrca/
│       │   ├── Dockerfile
│       │   ├── replayer.py
│       │   ├── time_mapper.py
│       │   ├── requirements.txt
│       │   └── README.md
│       ├── alibaba/
│       │   └── ...
│       └── acme/
│           └── ...
│
├── clients/
│   ├── static_rca_client.py              # NEW
│   └── (existing clients)
│
├── openrca_dataset/                      # Existing
│   ├── Bank/
│   │   ├── query.csv
│   │   ├── record.csv
│   │   └── telemetry/
│   ├── Market/
│   └── Telecom/
│
├── alibaba_cluster_dataset/              # Future
├── acme_cluster_dataset/                 # Future
│
└── docs/
    └── static-log-replayer-proposal.md   # This document
```

---

## Data Flow

### End-to-End Flow

```
[1] User starts experiment
    ↓
    python clients/static_rca_client.py --problem openrca_bank_task1
    ↓
[2] StaticOrchestrator.init_static_problem('openrca_bank_task1')
    ↓
[3] Load OpenRCABankTask1 problem
    ├─ Load config: configs/openrca_bank_task1.yaml
    ├─ Load ground truth: openrca_dataset/Bank/query.csv, record.csv
    └─ Initialize OpenRCAStaticApp
    ↓
[4] Start replayer Docker
    ├─ Mount: openrca_dataset/Bank → /datasets (read-only)
    ├─ Mount: aiopslab/data/telemetry_output/Bank → /telemetry_output (read-write)
    ├─ Mount: configs/openrca_bank_task1.yaml → /config/replayer_config.yaml
    └─ Run: aiopslab-static-replayer-openrca:latest
    ↓
[5] Replayer Phase 1: Bulk History Loading
    ├─ Read: /datasets/telemetry/2021_03_04/{log,trace,metric}/*.csv
    ├─ Filter: Data before simulation start_time (e.g., fault_time - 60 min)
    ├─ Remap timestamps: 2021-03-04 14:00 → 2026-02-10 10:00
    └─ Write: /telemetry_output/{trace,log,metric}.csv
    ↓
[6] Replayer Phase 2: Real-Time Streaming
    ├─ Stream data after start_time with timestamp offset
    └─ Append to CSV files (simulating live data generation)
    ↓
[7] Agent queries data
    ├─ Call: query_static_traces(start_time, end_time, cmdb_id='Tomcat01')
    ├─ StaticActions → OpenRCATraceAPI
    ├─ Read: /telemetry_output/trace.csv
    ├─ Filter: time window + cmdb_id
    └─ Return: DataFrame
    ↓
[8] Agent analyzes data and submits solution
    ├─ Agent identifies root cause
    └─ Call: submit_solution({'component': 'Redis02', 'reason': 'high memory usage'})
    ↓
[9] Evaluation
    ├─ OpenRCAEvaluator.evaluate_component_and_reason()
    ├─ Compare with ground truth
    └─ Return results
    ↓
[10] Cleanup
    └─ Stop replayer Docker container
```

---

## Configuration Design

### OpenRCA Config Example

```yaml
# aiopslab/service/apps/static_replayer/configs/openrca_bank_task1.yaml

# Dataset configuration
dataset:
  type: openrca
  namespace: Bank
  # Path is auto-resolved by OpenRCAStaticApp to:
  # {BASE_DIR}/openrca_dataset/Bank

# Simulation timing
simulation:
  # How to determine simulation start time
  start_time_option: fault_time  # Options: fault_time, fault_minus_5min, query_start, absolute

  # Offset in minutes (used with fault_time option)
  # Negative = start before fault, Positive = start after fault
  offset_minutes: -5

  # Absolute time (used with absolute option)
  # absolute_start_time: "2026-02-10 10:00:00"

# Time mapping (auto-populated by problem)
time_mapping:
  # From record.csv for this task
  historical_fault_time: "2021-03-04 14:57:00"

  # Auto-calculated based on start_time_option and offset_minutes
  simulation_start_time: null  # Will be set to "now" or specified time

# Telemetry configuration
telemetry:
  # Which telemetry types to replay
  enabled:
    - trace
    - log
    - metric

  # How much historical data to preload (minutes before start_time)
  history_window_minutes: 60

  # Streaming settings
  streaming:
    enabled: true
    # Speed multiplier (1.0 = real-time, 2.0 = 2x speed, 0 = instant)
    speed: 1.0

# Output configuration
output:
  # Path inside Docker container
  path: /telemetry_output

  # Output format
  format: csv

  # CSV options
  csv_options:
    include_header: true
    append_mode: true
```

### Start Time Options Explained

| Option | Description | Example |
|--------|-------------|---------|
| `fault_time` | Start at exact fault occurrence time + offset | Fault at 14:57, offset -5 → Start at 14:52 |
| `fault_minus_5min` | Shorthand for fault_time with -5 offset | Same as above |
| `query_start` | Use query time window start from query.csv | Query window 14:30-15:00 → Start at 14:30 |
| `absolute` | Use specified absolute timestamp | Start at 2026-02-10 10:00:00 |

---

## Docker Lifecycle Management

### Cleanup Strategy

**Problem**: After each experiment, we need to clean up Docker resources without wasting time rebuilding images.

**Solution**: **Stop Container + Clear Volume Data** (Recommended)

```
After each problem completion:
├─ [1] Stop container (fast, preserves image)
├─ [2] Clear volume data (delete CSV files)
├─ [3] Keep Docker image (reuse for next experiment)
└─ [4] Optional: Remove container (auto-remove flag)
```

**Why this approach**:
- ✅ **Fast**: No image rebuild needed (5-10 seconds vs 1-2 minutes)
- ✅ **Clean**: No data accumulation between experiments
- ✅ **Efficient**: Reuse image for multiple experiments
- ✅ **Developer-friendly**: Can inspect stopped containers for debugging
- ✅ **Disk-efficient**: Clear volume data prevents bloat

**Alternatives considered**:
| Approach | Speed | Cleanliness | Efficiency | Verdict |
|----------|-------|-------------|------------|---------|
| Stop only | ⚡ Fast | ⚠️ Data accumulates | ✅ Good | Not recommended |
| Delete all | 🐌 Slow | ✅ Very clean | ❌ Wasteful | Not recommended |
| **Stop + Clear volume** | ⚡ Fast | ✅ Clean | ✅ Excellent | **✅ Recommended** |

### Implementation

```python
# In BaseStaticApp
class BaseStaticApp:
    def cleanup(self):
        """
        Cleanup after problem completion.
        Called by orchestrator when problem ends.
        """
        logger.info("Starting cleanup...")

        # Step 1: Stop container
        if self.container:
            logger.info(f"Stopping container {self.container.id[:12]}...")
            self.container.stop(timeout=10)
            logger.info("Container stopped successfully")

        # Step 2: Clear volume data (keep directory structure)
        output_path = self.get_output_path()
        if output_path.exists():
            logger.info(f"Clearing volume data at {output_path}...")
            for csv_file in output_path.glob("*.csv"):
                logger.debug(f"Removing {csv_file.name}")
                csv_file.unlink()
            logger.info(f"Removed {len(list(output_path.glob('*.csv')))} CSV files")

        # Step 3: Image is kept automatically (no action needed)
        logger.info("Cleanup completed. Docker image preserved for reuse.")
```

```python
# In StaticOrchestrator
class StaticOrchestrator:
    def _cleanup_after_problem(self, prob):
        """Cleanup resources after problem completion."""
        try:
            logger.info("=" * 80)
            logger.info("CLEANUP: Starting post-experiment cleanup")
            logger.info("=" * 80)

            # Cleanup replayer
            prob.app.cleanup()

            logger.info("Cleanup successful")

        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)
            # Continue anyway, don't fail the experiment
```

### Docker Container Options

Run container with `--rm` flag for automatic removal:
```python
self.container = self.docker_client.containers.run(
    image=self.get_docker_image(),
    volumes=self.get_volumes(),
    detach=True,
    remove=True,  # Auto-remove container when stopped
    name=f"replayer-{self.config['dataset']['namespace']}-{timestamp}"
)
```

### Volume Management

**Volume structure**:
```
aiopslab/data/telemetry_output/
├─ Bank/                    # Namespace-specific
│   ├─ trace.csv           # Cleared after each experiment
│   ├─ log.csv
│   └─ metric.csv
├─ Telecom/
└─ Market/
```

**Cleanup options**:
```python
def clear_volume_data(self, output_path: Path):
    """Clear volume data with options."""

    # Option 1: Delete all CSV files
    for csv_file in output_path.glob("*.csv"):
        csv_file.unlink()

    # Option 2: Archive before delete (for debugging)
    if self.config.get('archive_telemetry'):
        archive_dir = output_path / "archive"
        archive_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for csv_file in output_path.glob("*.csv"):
            archived = archive_dir / f"{csv_file.stem}_{timestamp}.csv"
            csv_file.rename(archived)

    # Option 3: Keep for manual inspection (dev mode)
    if os.getenv('DEBUG_MODE') == 'true':
        logger.warning("DEBUG_MODE: Skipping volume cleanup")
        return
```

---

## Logging & Debugging Design

### Logging Philosophy

**Goals**:
- 🎯 **Developer-friendly**: Easy to understand what's happening
- 🔍 **Debuggable**: Rich context for troubleshooting
- 📊 **Monitorable**: Track progress and performance
- 🎨 **Readable**: Clear formatting with visual hierarchy

### Logging Levels

| Level | Usage | Example |
|-------|-------|---------|
| `DEBUG` | Detailed internal state | "Reading row 1000/5000 from trace.csv" |
| `INFO` | Key milestones & progress | "Replayer started successfully" |
| `WARNING` | Recoverable issues | "Missing optional metric file" |
| `ERROR` | Failures requiring attention | "Failed to connect to Docker" |
| `CRITICAL` | System-breaking errors | "Dataset not found" |

### Logging Structure

```python
# logging_config.py
import logging
import sys
from pathlib import Path
from datetime import datetime

def setup_logging(
    log_dir: Path = None,
    level: str = "INFO",
    session_id: str = None
):
    """
    Setup structured logging for static replayer system.

    Args:
        log_dir: Directory for log files (None = logs/ in project root)
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        session_id: Unique session ID for this experiment
    """

    # Create formatters
    detailed_formatter = logging.Formatter(
        fmt='%(asctime)s | %(name)-30s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    simple_formatter = logging.Formatter(
        fmt='[%(levelname)s] %(message)s'
    )

    # Console handler (simple format)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(simple_formatter)

    # File handler (detailed format)
    if log_dir is None:
        log_dir = Path.cwd() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_part = f"_{session_id}" if session_id else ""
    log_file = log_dir / f"static_replayer_{timestamp}{session_part}.log"

    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel("DEBUG")  # Always DEBUG in file
    file_handler.setFormatter(detailed_formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel("DEBUG")
    root_logger.handlers = []  # Clear existing handlers
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Suppress noisy libraries
    logging.getLogger("docker").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    return log_file


# Get logger for a module
logger = logging.getLogger(__name__)
```

### Logging at Key Steps

**1. Replayer Startup**:
```python
# replayer.py
import logging
logger = logging.getLogger(__name__)

def main():
    logger.info("=" * 80)
    logger.info("REPLAYER STARTUP")
    logger.info("=" * 80)

    # Load config
    logger.info("Loading configuration...")
    config = load_config('/config/replayer_config.yaml')
    logger.info(f"Dataset: {config['dataset']['type']} / {config['dataset']['namespace']}")
    logger.info(f"Start time option: {config['simulation']['start_time_option']}")
    logger.info(f"Enabled telemetry: {', '.join(config['telemetry']['enabled'])}")

    # Load dataset
    logger.info("Loading dataset from /datasets...")
    dataset = load_dataset(config['dataset']['path'])
    logger.info(f"Loaded {len(dataset.telemetry_files)} telemetry files")

    # Phase 1: Bulk history
    logger.info("=" * 80)
    logger.info("PHASE 1: BULK HISTORY LOADING")
    logger.info("=" * 80)

    start = time.time()
    history_data = load_history(dataset, config)
    logger.info(f"Loaded {len(history_data)} historical records in {time.time()-start:.2f}s")

    # Write to CSV
    logger.info("Writing history to CSV files...")
    for telemetry_type, data in history_data.items():
        output_file = f"/telemetry_output/{telemetry_type}.csv"
        write_csv(data, output_file)
        logger.info(f"  ✓ {telemetry_type}.csv: {len(data)} rows")

    logger.info("Phase 1 complete. History loaded successfully.")

    # Phase 2: Streaming
    logger.info("=" * 80)
    logger.info("PHASE 2: REAL-TIME STREAMING")
    logger.info("=" * 80)
    logger.info("Starting telemetry stream...")

    stream_telemetry(dataset, config)
```

**2. Static Observer Queries**:
```python
# openrca/trace_api.py
class OpenRCATraceAPI(StaticObserverBase):
    def extract_data(self, start_time, end_time, **filters):
        logger.debug(f"Query: start={start_time}, end={end_time}, filters={filters}")

        df = self._read_csv_with_time_filter(start_time, end_time)
        logger.debug(f"Read {len(df)} rows from CSV")

        # Apply filters
        if 'cmdb_id' in filters:
            df = df[df['cmdb_id'] == filters['cmdb_id']]
            logger.debug(f"After cmdb_id filter: {len(df)} rows")

        logger.info(f"Trace query returned {len(df)} spans")
        return df
```

**3. Static Orchestrator Flow**:
```python
# static_orchestrator.py
class StaticOrchestrator:
    def init_static_problem(self, problem_id: str):
        logger.info("=" * 80)
        logger.info(f"INITIALIZING PROBLEM: {problem_id}")
        logger.info("=" * 80)

        # Create session
        self.session = Session(results_dir=self.results_dir)
        session_id = self.session.session_id
        logger.info(f"Session ID: {session_id}")

        # Setup logging for this session
        log_file = setup_logging(session_id=session_id)
        logger.info(f"Logs: {log_file}")

        # Get problem
        logger.info(f"Loading problem: {problem_id}")
        prob = self.probs.get_problem_instance(problem_id)
        logger.info(f"Problem class: {prob.__class__.__name__}")
        logger.info(f"Dataset: {prob.app.config['dataset']['type']}")
        logger.info(f"Namespace: {prob.app.config['dataset']['namespace']}")

        # Start replayer
        logger.info("Starting replayer Docker container...")
        prob.start_replayer()
        logger.info("Replayer started successfully")

        # Wait for ready
        logger.info("Waiting for replayer to finish bulk loading...")
        prob._wait_for_replayer_ready(timeout=300)
        logger.info("Replayer ready. Telemetry data available.")

        return task_desc, instructions, actions
```

**4. Problem Evaluation**:
```python
# openrca/problems.py
class OpenRCABankTask1(BaseStaticProblem):
    def eval(self, soln, trace, duration):
        logger.info("=" * 80)
        logger.info("EVALUATION")
        logger.info("=" * 80)

        logger.info(f"Agent solution: {soln}")
        logger.info(f"Ground truth: {self.ground_truth['timestamp']}")

        results = self.evaluator.evaluate_time_detection(
            predicted_time=soln,
            ground_truth_time=self.ground_truth['timestamp'],
            threshold_minutes=1
        )

        logger.info(f"Time error: {results['time_error_minutes']:.2f} minutes")
        logger.info(f"Success: {results['success']}")
        logger.info(f"Duration: {duration:.2f}s")

        return results
```

### Visual Progress Indicators

```python
# For long-running operations
from tqdm import tqdm

def load_history(dataset, config):
    logger.info("Loading historical telemetry...")

    telemetry_files = dataset.get_telemetry_files()

    with tqdm(total=len(telemetry_files), desc="Loading files") as pbar:
        for file in telemetry_files:
            data = load_telemetry_file(file)
            pbar.update(1)

    logger.info(f"Loaded {total_rows} rows")
```

### Error Context

```python
# Rich error messages with context
try:
    df = pd.read_csv(csv_path)
except FileNotFoundError as e:
    logger.error(
        f"Telemetry file not found!\n"
        f"  Expected: {csv_path}\n"
        f"  Problem: {problem_id}\n"
        f"  Dataset: {dataset_type}/{namespace}\n"
        f"  Hint: Has the replayer finished bulk loading?",
        exc_info=True
    )
    raise
```

### Debug Mode

```python
# Enable debug mode via environment variable
if os.getenv('DEBUG_MODE') == 'true':
    # More verbose logging
    logger.setLevel(logging.DEBUG)

    # Save intermediate data
    df.to_csv('/tmp/debug_trace_query.csv', index=False)
    logger.debug(f"Saved debug data to /tmp/debug_trace_query.csv")

    # Don't cleanup
    logger.warning("DEBUG_MODE: Skipping cleanup")
```

---

## Results File Format

### Design Principles

**Goals**:
- 📊 **Easy to parse**: Machine-readable (JSON) and human-readable
- 📈 **Easy to analyze**: Tabular format (CSV) for aggregation
- 🔍 **Rich metadata**: Experiment context preserved
- 📁 **Well-organized**: Hierarchical directory structure

### Directory Structure

```
results/
└─ static_rca/
    ├─ experiments.csv                    # Summary of all experiments
    ├─ openrca_bank/
    │   ├─ task1/
    │   │   ├─ 20260210_100530_abc123/   # Timestamped session directory
    │   │   │   ├─ metadata.json         # Experiment metadata
    │   │   │   ├─ results.json          # Evaluation results
    │   │   │   ├─ trace.json            # Agent conversation trace
    │   │   │   ├─ telemetry_stats.json  # Dataset statistics
    │   │   │   └─ logs/
    │   │   │       └─ replayer.log
    │   │   ├─ 20260210_103045_def456/
    │   │   └─ ...
    │   ├─ task6/
    │   └─ ...
    ├─ openrca_telecom/
    └─ ...
```

### Metadata Format

```json
{
  "metadata": {
    "session_id": "abc123",
    "timestamp_start": "2026-02-10T10:05:30.123456",
    "timestamp_end": "2026-02-10T10:07:45.654321",
    "duration_seconds": 135.53,

    "problem": {
      "id": "openrca_bank_task1",
      "type": "time_detection",
      "dataset": "openrca",
      "namespace": "Bank",
      "task_index": "task_1",
      "task_number": 0,
      "description": "On March 4, 2021, within the time range of 14:30 to 15:00..."
    },

    "agent": {
      "type": "gpt",
      "model": "gpt-4",
      "name": "GPTClient",
      "config": {
        "temperature": 0.7,
        "max_tokens": 2000
      }
    },

    "simulation": {
      "start_time_option": "fault_time",
      "offset_minutes": -5,
      "historical_fault_time": "2021-03-04T14:57:00",
      "simulation_start_time": "2026-02-10T10:00:00",
      "history_window_minutes": 60,
      "enabled_telemetry": ["trace", "log", "metric"]
    },

    "environment": {
      "python_version": "3.11.5",
      "aiopslab_version": "1.0.0",
      "hostname": "macbook-pro.local",
      "platform": "darwin"
    }
  }
}
```

### Results Format

```json
{
  "results": {
    "success": true,

    "prediction": {
      "root_cause_time": "2026-02-10T10:57:00",
      "root_cause_component": null,
      "root_cause_reason": null
    },

    "ground_truth": {
      "root_cause_time": "2026-02-10T10:57:00",
      "root_cause_component": "Mysql02",
      "root_cause_reason": "high memory usage"
    },

    "evaluation": {
      "time_accuracy": "Correct",
      "time_error_minutes": 0.5,
      "component_accuracy": null,
      "reason_accuracy": null,
      "combined_success": true
    },

    "metrics": {
      "duration_seconds": 135.53,
      "num_agent_steps": 8,
      "num_queries": {
        "trace": 3,
        "log": 4,
        "metric": 5
      },
      "telemetry_data_queried": {
        "trace_spans": 15432,
        "log_lines": 8765,
        "metric_points": 23456
      },
      "tokens": {
        "input": 12543,
        "output": 3421,
        "total": 15964
      }
    },

    "timeline": [
      {
        "step": 1,
        "timestamp": "2026-02-10T10:05:32",
        "action": "get_available_services",
        "result_summary": "Found 12 services"
      },
      {
        "step": 2,
        "timestamp": "2026-02-10T10:05:45",
        "action": "query_static_logs",
        "args": {
          "start_time": "2026-02-10T10:00:00",
          "end_time": "2026-02-10T10:30:00",
          "keyword": "error"
        },
        "result_summary": "Found 234 error logs"
      }
    ]
  }
}
```

### Experiments Summary CSV

Auto-generated summary of all experiments for easy analysis:

```csv
session_id,timestamp,dataset,namespace,task_type,agent_type,success,duration_sec,time_error_min,component_correct,reason_correct,num_steps,total_tokens,notes
abc123,2026-02-10T10:05:30,openrca,Bank,time_detection,gpt,true,135.53,0.5,,,8,15964,
def456,2026-02-10T10:30:15,openrca,Bank,component_reason,gpt,true,187.32,,true,true,12,23451,
ghi789,2026-02-10T11:00:00,openrca,Telecom,time_detection,claude,false,98.21,5.3,,,6,12334,Timeout issue
```

### Result Writer

```python
# results_writer.py
import json
from pathlib import Path
from datetime import datetime
import pandas as pd

class ResultsWriter:
    """Write experiment results in structured format."""

    def __init__(self, results_dir: Path):
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)

        # Summary CSV file
        self.summary_file = self.results_dir / "experiments.csv"

    def create_session_directory(self, problem_id: str, session_id: str) -> Path:
        """
        Create timestamped session directory.

        Returns:
            Path to session directory
        """
        # Parse problem_id: openrca_bank_task1 -> openrca_bank/task1/
        parts = problem_id.split('_')
        dataset_namespace = '_'.join(parts[:-1])  # openrca_bank
        task = parts[-1]  # task1

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_dir = (
            self.results_dir
            / dataset_namespace
            / task
            / f"{timestamp}_{session_id}"
        )

        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "logs").mkdir(exist_ok=True)

        logger.info(f"Session directory: {session_dir}")
        return session_dir

    def write_metadata(self, session_dir: Path, metadata: dict):
        """Write metadata.json"""
        with open(session_dir / "metadata.json", 'w') as f:
            json.dump({"metadata": metadata}, f, indent=2, default=str)

        logger.debug("Wrote metadata.json")

    def write_results(self, session_dir: Path, results: dict):
        """Write results.json"""
        with open(session_dir / "results.json", 'w') as f:
            json.dump({"results": results}, f, indent=2, default=str)

        logger.info("Wrote results.json")

    def write_trace(self, session_dir: Path, trace: list):
        """Write conversation trace"""
        with open(session_dir / "trace.json", 'w') as f:
            json.dump({"trace": trace}, f, indent=2, default=str)

        logger.debug("Wrote trace.json")

    def append_to_summary(self, metadata: dict, results: dict):
        """Append experiment to summary CSV"""
        row = {
            'session_id': metadata['session_id'],
            'timestamp': metadata['timestamp_start'],
            'dataset': metadata['problem']['dataset'],
            'namespace': metadata['problem']['namespace'],
            'task_type': metadata['problem']['type'],
            'agent_type': metadata['agent']['type'],
            'success': results.get('success', False),
            'duration_sec': metadata['duration_seconds'],
            'time_error_min': results.get('evaluation', {}).get('time_error_minutes'),
            'component_correct': results.get('evaluation', {}).get('component_accuracy') == 'Correct',
            'reason_correct': results.get('evaluation', {}).get('reason_accuracy') == 'Correct',
            'num_steps': results.get('metrics', {}).get('num_agent_steps'),
            'total_tokens': results.get('metrics', {}).get('tokens', {}).get('total'),
            'notes': ''
        }

        # Append to CSV
        df = pd.DataFrame([row])
        df.to_csv(
            self.summary_file,
            mode='a',
            header=not self.summary_file.exists(),
            index=False
        )

        logger.info(f"Updated summary: {self.summary_file}")
```

### Usage in Orchestrator

```python
# In StaticOrchestrator
def init_static_problem(self, problem_id: str):
    # ... existing code ...

    # Create results directory for this session
    self.results_writer = ResultsWriter(self.results_dir)
    self.session_dir = self.results_writer.create_session_directory(
        problem_id, self.session.session_id
    )

    # Write metadata
    metadata = {
        'session_id': self.session.session_id,
        'timestamp_start': datetime.now().isoformat(),
        'problem': {
            'id': problem_id,
            'dataset': prob.app.config['dataset']['type'],
            'namespace': prob.app.config['dataset']['namespace'],
            # ... more fields
        },
        # ... more metadata
    }
    self.results_writer.write_metadata(self.session_dir, metadata)

    return task_desc, instructions, actions


def submit_solution(self, solution) -> dict:
    # ... existing evaluation code ...

    # Write results
    self.results_writer.write_results(self.session_dir, results)
    self.results_writer.write_trace(self.session_dir, trace)
    self.results_writer.append_to_summary(metadata, results)

    logger.info(f"Results saved to: {self.session_dir}")

    return results
```

---

## Implementation Plan

### Phase 1: OpenRCA Foundation (Weeks 1-2)

**Goal**: End-to-end working system for OpenRCA Bank dataset

**Tasks**:
1. ✅ Create directory structure
2. ✅ Implement Replayer Docker for OpenRCA
   - `replayer.py`: Core replayer logic
   - `time_mapper.py`: Timestamp remapping
   - `Dockerfile`: Container definition
3. ✅ Implement Static Observers for OpenRCA
   - `base.py`: Base classes
   - `openrca/trace_api.py`
   - `openrca/log_api.py`
   - `openrca/metric_api.py`
4. ✅ Implement Static Application
   - `base.py`: BaseStaticApp
   - `openrca.py`: OpenRCAStaticApp
5. ✅ Implement Static Actions
   - `static_actions.py`: Dataset-agnostic query actions
6. ✅ Implement Static Orchestrator
   - `static_orchestrator.py`: Separate from live orchestrator
7. ✅ Implement Static Problems for OpenRCA
   - `base.py`: BaseStaticProblem
   - `openrca/loader.py`: Load query.csv, record.csv
   - `openrca/evaluator.py`: OpenRCA-specific evaluation
   - `openrca/problems.py`: Task 1 and Task 6 implementations
8. ✅ Implement Client
   - `static_rca_client.py`: Simple test client
9. ✅ Testing
   - Test replayer Docker build and run
   - Test end-to-end flow with one task
   - Verify timestamp remapping correctness

**Deliverables**:
- Working system for OpenRCA Bank Task 1 and Task 6
- Documentation for adding new OpenRCA tasks

---

### Phase 2: Expandability (Weeks 3-4)

**Goal**: Add more OpenRCA namespaces and task types

**Tasks**:
1. ✅ Add OpenRCA Telecom support
   - Config: `openrca_telecom.yaml`
   - Problems: Telecom-specific tasks
2. ✅ Add OpenRCA Market support
   - Config: `openrca_market_cloudbed1.yaml`, `openrca_market_cloudbed2.yaml`
   - Problems: Market-specific tasks
3. ✅ Implement all OpenRCA task types
   - Task 1: Root cause time detection
   - Task 5: Root cause time + component detection
   - Task 6: Root cause component + reason detection
   - Task 7: Full RCA (time + component + reason)
4. ✅ Comprehensive testing
   - Test all task types
   - Test all namespaces
   - Benchmark evaluation accuracy

**Deliverables**:
- Complete OpenRCA support (all namespaces, all task types)
- Evaluation benchmark results

---

### Phase 3: Alibaba Dataset (Weeks 5-6)

**Goal**: Add Alibaba cluster dataset support

**Tasks**:
1. ✅ Study Alibaba dataset format
   - Understand schema differences
   - Identify root cause types
2. ✅ Implement Alibaba Replayer
   - `alibaba/replayer.py`
   - `alibaba/Dockerfile`
3. ✅ Implement Alibaba Static Observers
   - `alibaba/trace_api.py`
   - `alibaba/log_api.py`
   - `alibaba/metric_api.py`
4. ✅ Implement Alibaba Static App
   - `alibaba.py`: AlibabaStaticApp
   - `configs/alibaba_*.yaml`
5. ✅ Implement Alibaba Problems
   - `alibaba/loader.py`
   - `alibaba/evaluator.py`
   - `alibaba/problems.py`
6. ✅ Testing

**Deliverables**:
- Full Alibaba dataset support
- Documentation for dataset-specific differences

---

### Phase 4: ACME Dataset (Weeks 7-8)

**Goal**: Add ACME cluster dataset support

**Tasks**:
- Same structure as Phase 3, but for ACME dataset

**Deliverables**:
- Full ACME dataset support
- Multi-dataset comparison benchmarks

---

### Phase 5: Advanced Features (Weeks 9-10)

**Goal**: Add advanced features and optimizations

**Tasks**:
1. ✅ Agent action helpers
   - Anomaly detection helpers
   - Correlation analysis helpers
2. ✅ Performance optimizations
   - Lazy CSV loading
   - Caching frequently accessed data
3. ✅ Visualization tools
   - Plot telemetry timeseries
   - Visualize root cause analysis
4. ✅ Batch evaluation
   - Run multiple experiments in parallel
   - Aggregate results
5. ✅ Documentation
   - User guide
   - Developer guide for adding new datasets

**Deliverables**:
- Production-ready system
- Complete documentation

---

## Expandability Strategy

### Adding a New Dataset

To add a new dataset (e.g., "FooBar"), follow these steps:

**1. Create Replayer**:
```bash
mkdir -p aiopslab-applications/static-replayers/foobar
cd aiopslab-applications/static-replayers/foobar
# Create: Dockerfile, replayer.py, time_mapper.py
```

**2. Create Static Observers**:
```bash
mkdir -p aiopslab/observer/static/foobar
cd aiopslab/observer/static/foobar
# Create: __init__.py, trace_api.py, log_api.py, metric_api.py
```

**3. Create Static App**:
```python
# aiopslab/service/apps/static_replayer/foobar.py
from aiopslab.service.apps.static_replayer.base import BaseStaticApp

class FooBarStaticApp(BaseStaticApp):
    def get_dataset_path(self):
        return BASE_DIR / f"foobar_dataset/{self.config['dataset']['namespace']}"

    def get_docker_image(self):
        return "aiopslab-static-replayer-foobar:latest"

    # ... implement abstract methods
```

**4. Create Static Problems**:
```bash
mkdir -p aiopslab/orchestrator/static_problems/foobar
cd aiopslab/orchestrator/static_problems/foobar
# Create: __init__.py, problems.py, evaluator.py, loader.py
```

**5. Register Problems**:
```python
# In aiopslab/orchestrator/static_problems/registry.py
from aiopslab.orchestrator.static_problems.foobar.problems import FooBarTask1

self.register('foobar_task1', FooBarTask1)
```

**6. Create Configs**:
```bash
# aiopslab/service/apps/static_replayer/configs/foobar_*.yaml
```

**7. Test**:
```bash
python clients/static_rca_client.py --problem foobar_task1
```

---

## Open Questions

### Technical Questions

1. **Timestamp Precision**: Should we support sub-second timestamps? OpenRCA uses seconds, but other datasets might need milliseconds.
   - **Recommendation**: Use float timestamps (Unix epoch with decimals) for maximum flexibility

2. **CSV Size Management**: Large CSV files might cause memory issues. Should we implement chunking or streaming reads?
   - **Recommendation**: Phase 1 uses simple pandas read, Phase 5 adds chunking if needed

3. **Replayer State Management**: Should replayer save checkpoints to resume from failures?
   - **Recommendation**: Phase 1 is stateless (just write CSVs), Phase 5 can add checkpointing

4. **Multi-Tenancy**: Should one replayer instance support multiple concurrent experiments?
   - **Recommendation**: Phase 1 is single-tenant (one experiment per replayer), Phase 5 can add multi-tenancy

### Dataset-Specific Questions

1. **OpenRCA Task Parsing**: The current query.csv embeds time windows in free text. Should we create a parser or manually create configs?
   - **Recommendation**: Phase 1 uses manual configs, Phase 2 implements automatic parser

2. **Alibaba Schema**: What is the exact schema for Alibaba dataset? Need to study the dataset structure.
   - **Action Item**: Study `docs/alibaba-cluster-trace-gpu-2020.md`

3. **ACME Schema**: Same question for ACME dataset.
   - **Action Item**: Study ACME dataset structure

### Evaluation Questions

1. **Partial Credit**: Should we give partial credit for close-but-not-exact answers (e.g., 2 minutes off instead of 1)?
   - **Recommendation**: Phase 1 uses strict thresholds (from OpenRCA scoring), Phase 2 can add partial credit

2. **Multi-Metric Evaluation**: Should we evaluate on multiple dimensions (accuracy, speed, token usage, etc.)?
   - **Recommendation**: Yes, track all metrics from the start

---

## Success Criteria

### Phase 1 (OpenRCA Foundation)
- ✅ Replayer Docker successfully builds and runs
- ✅ Telemetry CSV files are generated with correct timestamps
- ✅ Static observers can query data
- ✅ Agent can solve at least one OpenRCA task
- ✅ Evaluation produces expected results
- ✅ Cleanup works correctly (container stops, volume clears, image preserved)
- ✅ Logging is clear and helpful for debugging
- ✅ Results are saved in structured format with metadata

### Full System
- ✅ Support all 3 datasets (OpenRCA, Alibaba, ACME)
- ✅ Support all OpenRCA task types
- ✅ Code is clean, modular, and well-documented
- ✅ Easy to add new datasets (< 1 day of work)
- ✅ Performance: Can run experiments in < 5 minutes
- ✅ Accuracy: Match or exceed baseline RCA agent performance
- ✅ Developer-friendly: Easy to debug with logs and structured results
- ✅ Experiments CSV provides quick overview of all runs

---

## Conclusion

This proposal outlines a comprehensive design for a Static Log Replayer system that:

1. **Separates concerns**: Replayer, Observer, Orchestrator, Problems, Actions
2. **Push-based architecture**: Simple CSV writing for telemetry replay
3. **Smart cleanup**: Stop + clear volume strategy for efficiency
4. **Dataset-organized**: Problems grouped by dataset for clarity
5. **Expandable**: Plugin architecture for new datasets
6. **Isolated**: Agents can't access raw datasets, only replayed data
7. **Developer-friendly**: Comprehensive logging, structured results, easy debugging
8. **Testable**: Start with OpenRCA, incrementally add complexity

The implementation plan spans 10 weeks with 5 phases, starting with a working OpenRCA system and expanding to support multiple datasets.

**Next Steps**:
1. Review and approve this proposal
2. Begin Phase 1 implementation
3. Iterate based on findings from initial testing

---

## Appendix

### Example Commands

```bash
# Build OpenRCA replayer Docker image
cd aiopslab-applications/static-replayers/openrca
docker build -t aiopslab-static-replayer-openrca:latest .

# Run a static RCA experiment
python clients/static_rca_client.py --problem openrca_bank_task1 --agent gpt

# List available problems
python -c "from aiopslab.orchestrator.static_problems.registry import StaticProblemRegistry; print(StaticProblemRegistry().list_problems())"

# Start replayer manually for debugging
docker run -it --rm \
  -v ./openrca_dataset/Bank:/datasets:ro \
  -v ./aiopslab/data/telemetry_output/Bank:/telemetry_output \
  -v ./configs/openrca_bank.yaml:/config/replayer_config.yaml:ro \
  aiopslab-static-replayer-openrca:latest
```

### Reference Links

- OpenRCA Dataset: [Link if available]
- Alibaba Cluster Trace: `docs/alibaba-cluster-trace-gpu-2020.md`
- AIOpsLab Architecture: `docs/aiopslab-*.md`
