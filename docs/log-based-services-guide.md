# Log-Based Services and Time-Elapsed Guide

This guide explains how to change AIOpsLab services to be log-based and add time-elapsed functionality.

---

## Current Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Orchestrator                             │
│  - Manages session timing (start/end)                           │
│  - Calculates framework overhead                                 │
│  - Tracks TTD/TTL/TTA/TTM                                       │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                         Task Actions                             │
│  - get_logs()     → Collects logs via kubectl/docker            │
│  - get_metrics()  → Collects metrics via Prometheus             │
│  - get_traces()   → Collects traces via Jaeger                  │
│  - exec_shell()   → Execute shell commands                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. Making Services Log-Based

### Option A: Add Log-Based Actions to TaskActions

Edit `aiopslab/orchestrator/actions/base.py`:

```python
import time
from datetime import datetime, timedelta
from aiopslab.observer.log_api import LogAPI
from aiopslab.observer import monitor_config

class TaskActions:
    """Base class for task actions with log-based support."""

    # Add class-level timing tracker
    _action_timings = {}

    @staticmethod
    @read
    def get_logs_elasticsearch(
        namespace: str,
        service: str = None,
        duration: int = 5
    ) -> str:
        """
        Collects logs from Elasticsearch for a specified time window.

        Args:
            namespace (str): The namespace in which the service is running.
            service (str): Optional service name to filter logs.
            duration (int): Minutes of logs to collect (default: 5).

        Returns:
            str: Path to saved log file or log content.
        """
        log_api = LogAPI(
            monitor_config["api"],
            monitor_config["username"],
            monitor_config["password"]
        )

        end_time = datetime.now()
        start_time = end_time - timedelta(minutes=duration)
        save_path = os.path.join(os.getcwd(), "log_output")
        os.makedirs(save_path, exist_ok=True)

        log_api.log_extract(
            start_time=int(start_time.timestamp()),
            end_time=int(end_time.timestamp()),
            path=save_path
        )

        return f"Logs exported to: {save_path}"

    @staticmethod
    @read
    def read_logs(file_path: str) -> str:
        """
        Reads and returns logs from a specified CSV file.

        Args:
            file_path (str): Path to the log file (CSV format).

        Returns:
            str: The log content or an error message.
        """
        if not os.path.exists(file_path):
            return f"Error: Log file '{file_path}' not found."

        try:
            df_logs = pd.read_csv(file_path)
            return df_logs.to_string(index=False)
        except Exception as e:
            return f"Failed to read logs: {str(e)}"

    @staticmethod
    @read
    def query_logs(
        namespace: str,
        start_time: int,
        end_time: int,
        service: str = None
    ) -> str:
        """
        Query logs from Elasticsearch for a specific time range.

        Args:
            namespace (str): The namespace to query.
            start_time (int): Start timestamp (Unix).
            end_time (int): End timestamp (Unix).
            service (str): Optional service name filter.

        Returns:
            str: Formatted log data.
        """
        log_api = LogAPI(
            monitor_config["api"],
            monitor_config["username"],
            monitor_config["password"]
        )

        logs = log_api.query(start_time, end_time)

        if service:
            logs = [
                log for log in logs
                if service in log.get("_source", {}).get("kubernetes", {}).get("pod", {}).get("name", "")
            ]

        # Format logs for output
        formatted = []
        for log in logs[:100]:  # Limit to 100 logs
            source = log.get("_source", {})
            formatted.append({
                "timestamp": source.get("@timestamp"),
                "pod": source.get("kubernetes", {}).get("pod", {}).get("name"),
                "message": source.get("message", "")[:200]
            })

        return str(formatted)
```

### Option B: Create a New Log-Based Task Type

Create `aiopslab/orchestrator/tasks/log_based.py`:

```python
from aiopslab.orchestrator.tasks.base import Task
from aiopslab.utils.actions import get_actions

class LogBasedTask(Task):
    """Task type that uses log-based telemetry for evaluation."""

    def __init__(self):
        super().__init__()
        self.telemetry_type = "logs"

    def get_available_actions(self) -> dict:
        """Return only log-based actions."""
        actions = get_actions("base", subtype="read")
        # Filter to log-related actions
        log_actions = {
            k: v for k, v in actions.items()
            if "log" in k.lower()
        }
        return log_actions
```

---

## 2. Adding Time-Elapsed Functionality

### Option A: Add to Session Class

Edit `aiopslab/session.py`:

```python
import time

class Session:
    def __init__(self, results_dir=None) -> None:
        # ... existing init ...
        self.action_timings = {}  # Track individual action timings
        self._last_action_start = None
        self._current_action = None

    def start_action(self, action_name: str):
        """Start timing an action."""
        self._current_action = action_name
        self._last_action_start = time.time()

    def end_action(self) -> float:
        """End timing the current action and return elapsed time."""
        if self._last_action_start is None:
            return 0.0

        elapsed = time.time() - self._last_action_start

        if self._current_action:
            if self._current_action not in self.action_timings:
                self.action_timings[self._current_action] = []
            self.action_timings[self._current_action].append(elapsed)

        self._last_action_start = None
        self._current_action = None
        return elapsed

    def get_elapsed(self) -> float:
        """Get elapsed time since session started."""
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time

    def get_action_stats(self) -> dict:
        """Get statistics for all action timings."""
        stats = {}
        for action, times in self.action_timings.items():
            stats[action] = {
                "count": len(times),
                "total": sum(times),
                "avg": sum(times) / len(times) if times else 0,
                "min": min(times) if times else 0,
                "max": max(times) if times else 0,
            }
        return stats

    def to_dict(self):
        """Return the session history as a dictionary."""
        summary = {
            "agent": self.agent_name,
            "session_id": str(self.session_id),
            "problem_id": self.pid,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.get_duration() if self.end_time else None,
            "action_timings": self.get_action_stats(),  # NEW
            "trace": [item.model_dump() for item in self.history],
            "results": self.results,
        }
        return summary
```

### Option B: Add Timing Decorator to Actions

Create `aiopslab/utils/timing.py`:

```python
import time
import functools
from typing import Callable

# Global timing storage
_action_timings = {}

def timed_action(func: Callable) -> Callable:
    """
    Decorator that tracks execution time of actions.

    Usage:
        @timed_action
        @action
        def my_action(...):
            ...
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        elapsed = time.time() - start_time

        action_name = func.__name__
        if action_name not in _action_timings:
            _action_timings[action_name] = []
        _action_timings[action_name].append(elapsed)

        # Optionally add timing info to result
        print(f"[TIMING] {action_name}: {elapsed:.3f}s")

        return result

    wrapper.is_action = getattr(func, 'is_action', False)
    wrapper.action_type = getattr(func, 'action_type', None)
    return wrapper


def get_timing_stats() -> dict:
    """Get all action timing statistics."""
    stats = {}
    for action, times in _action_timings.items():
        stats[action] = {
            "count": len(times),
            "total_seconds": sum(times),
            "avg_seconds": sum(times) / len(times) if times else 0,
            "min_seconds": min(times) if times else 0,
            "max_seconds": max(times) if times else 0,
        }
    return stats


def reset_timings():
    """Reset all timing data."""
    global _action_timings
    _action_timings = {}


class TimeElapsed:
    """Context manager for tracking elapsed time."""

    def __init__(self, label: str = None):
        self.label = label
        self.start_time = None
        self.elapsed = None

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, *args):
        self.elapsed = time.time() - self.start_time
        if self.label:
            print(f"[ELAPSED] {self.label}: {self.elapsed:.3f}s")

    def get_elapsed(self) -> float:
        """Get current elapsed time (can be called during execution)."""
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time
```

### Option C: Integrate Time-Elapsed into Orchestrator

Edit `aiopslab/orchestrator/orchestrator.py`:

```python
class Orchestrator:
    def __init__(self, results_dir=None):
        # ... existing init ...
        self.step_timings = []  # Track each step's timing

    async def start_problem(self, max_steps: int):
        """Start the task with step-by-step timing."""
        assert self.session is not None
        action_instr = "Please take the next action"
        action, env_response, results = "", "", {}
        self.session.start()
        self.step_timings = []

        try:
            for step in range(max_steps):
                step_start = time.time()

                # Time agent thinking
                agent_start = time.time()
                action = await self.ask_agent(action_instr)
                agent_time = time.time() - agent_start
                self.sprint.agent(action)

                # Time environment response
                env_start = time.time()
                env_response = await self.ask_env(action)
                env_time = time.time() - env_start
                self.sprint.service(env_response)

                # Record step timing
                step_total = time.time() - step_start
                self.step_timings.append({
                    "step": step,
                    "agent_time": agent_time,
                    "env_time": env_time,
                    "total_time": step_total,
                    "elapsed_since_start": time.time() - self.session.start_time,
                })

                # Print elapsed time
                elapsed = time.time() - self.session.start_time
                print(f"[Step {step}] Elapsed: {elapsed:.2f}s (Agent: {agent_time:.2f}s, Env: {env_time:.2f}s)")

                if env_response == SubmissionStatus.VALID_SUBMISSION:
                    break
                # ... rest of loop

        # ... rest of method

        # Add step timings to results
        return {
            "history": self.session.history,
            "final_state": env_response,
            "results": results,
            "framework_overhead": framework_overhead,
            "step_timings": self.step_timings,  # NEW
        }
```

---

## 3. Usage Examples

### Using Timed Actions

```python
from aiopslab.utils.timing import timed_action, TimeElapsed, get_timing_stats

# Apply to existing actions
class MyActions(TaskActions):
    @staticmethod
    @timed_action
    @read
    def get_logs(namespace: str, service: str) -> str:
        # ... implementation
        pass

# Using context manager
with TimeElapsed("Log Collection") as timer:
    logs = collect_logs(start_time, end_time)
    print(f"Current elapsed: {timer.get_elapsed():.2f}s")

print(f"Total elapsed: {timer.elapsed:.2f}s")

# Get all timing stats
stats = get_timing_stats()
print(stats)
```

### Using Session Timing

```python
session = Session()
session.start()

# Track action timing
session.start_action("get_logs")
logs = task.get_logs(namespace, service)
elapsed = session.end_action()
print(f"get_logs took {elapsed:.2f}s")

# Get elapsed since session start
print(f"Session elapsed: {session.get_elapsed():.2f}s")

session.end()
print(session.get_action_stats())
```

---

## 4. File Changes Summary

| File | Change |
|------|--------|
| `aiopslab/session.py` | Add `start_action()`, `end_action()`, `get_elapsed()`, `get_action_stats()` |
| `aiopslab/utils/timing.py` | **NEW** - Add timing utilities |
| `aiopslab/orchestrator/actions/base.py` | Add `get_logs_elasticsearch()`, `read_logs()`, `query_logs()` |
| `aiopslab/orchestrator/orchestrator.py` | Add step-by-step timing in `start_problem()` |
| `aiopslab/observer/log_api.py` | Already has log collection (Elasticsearch) |

---

## 5. Configuration for Log-Based Services

Edit `aiopslab/config/monitor_config.yaml`:

```yaml
# Elasticsearch configuration
api: "https://your-elasticsearch-url:9200"
username: "elastic"
password: "your-password"
es_use_cert: "False"
es_cert_path: ""

# Namespace for services
namespace: "test-hotel-reservation"

# Log-based settings
log_collection:
  enabled: true
  source: "elasticsearch"  # or "loki"
  default_duration_minutes: 5
  max_logs_per_query: 7500

# Timing settings
timing:
  track_actions: true
  track_steps: true
  verbose: true
```

---

## 6. Quick Implementation Checklist

- [ ] Add timing utilities (`aiopslab/utils/timing.py`)
- [ ] Update Session class with timing methods
- [ ] Add log-based actions to TaskActions
- [ ] Update Orchestrator to track step timings
- [ ] Configure Elasticsearch/Loki connection
- [ ] Test with a sample problem
