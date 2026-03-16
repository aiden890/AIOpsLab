"""ReAct agent client for RE2-TT Root Cause Analysis.

Contains the Agent class, prompt templates, and scoring utilities.
Import this module from a run script — do not run directly.
"""

import csv
import sys
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import tiktoken
from clients.utils.llm import GPTClient
from clients.utils.templates import DOCS_TT_RCA

logger = logging.getLogger("react_tt")

# ---------------------------------------------------------------------------
# Prompt constants
# ---------------------------------------------------------------------------

RESP_INSTR = """Please avoid repeating previous actions. Respond with:
Thought: <your analysis of the previous output>
Action:
```
<api_call(args)>
```
"""

ALLOWED_ACTIONS = {
    "execute",
    "get_logs", "get_metrics", "get_traces",
    "read_logs", "read_metrics", "read_traces",
    "submit",
}

# Domain schema for the Executor sub-LLM (code generation context)
TT_SCHEMA = """\
## TELEMETRY DATA ACCESS:

- Use `telemetry.get_logs()`, `telemetry.get_metrics()`, `telemetry.get_traces()` to fetch data.
- Each returns a file path to a CSV. Read with `pd.read_csv(telemetry.get_metrics())`.

## DATA SCHEMA

`get_metrics()` returns a path to a single merged CSV. Load it with:

```python
import pandas as pd
df = pd.read_csv(telemetry.get_metrics())
```

The merged DataFrame has one row per 15-second timestamp (`time` in Unix seconds).
Columns come from four sources, distinguished by naming convention:

- `{service}_cpu`, `{service}_mem`, `{service}_diskio`, `{service}_socket`
  `{service}_latency-50`, `{service}_latency-90`, `{service}_error`, `{service}_workload`
    → resource metrics per service (from simple_metrics.csv)
- `{service}_{template_id}_logcount`
    → log event count per log template per service (from logts.csv).
    Each service has multiple template IDs. To get total log activity for a service,
    sum all columns matching that service name.
    Example: `df.filter(like='ts-auth-service').filter(like='_logcount').sum(axis=1)`
- `lat_{service}_{operation}`
    → mean trace latency in ms for that operation (from tracets_lat.csv)
    Example column: `lat_ts-auth-service_POST-users-login`
- `err_{service}_{operation}`
    → trace error rate 0–1 for that operation (from tracets_err.csv)
    Example column: `err_ts-auth-service_POST-users-login`

To isolate signals per service:
```python
# CPU metrics for ts-auth-service
cpu_col = [c for c in df.columns if 'ts-auth-service' in c and c.endswith('_cpu')]
# Disk I/O for ts-auth-service (column name: ts-auth-service_diskio)
diskio_col = [c for c in df.columns if 'ts-auth-service' in c and c.endswith('_diskio')]
# Socket for ts-auth-service
socket_col = [c for c in df.columns if 'ts-auth-service' in c and c.endswith('_socket')]
# Total log activity for ts-auth-service
log_cols = [c for c in df.columns if 'ts-auth-service' in c and '_logcount' in c]
# Trace error rate columns for ts-auth-service
err_cols = [c for c in df.columns if c.startswith('err_ts-auth-service')]
# Trace latency columns for ts-auth-service
lat_cols = [c for c in df.columns if c.startswith('lat_ts-auth-service')]
```

**Trace columns** (traces.csv):
```
traceID, spanID, parentSpanID, serviceName, operationName, startTimeMillis, duration, statusCode
```
`startTimeMillis` in ms, `duration` in microseconds.

**Log columns** (logs.csv):
```
time, timestamp (ns), container_name, message, level, req_path, error
```
NOTE: The "level" and "error" columns are usually EMPTY (NaN).
Log level is embedded in the "message" text, e.g.: "2024-01-22 10:59:05  INFO 1 --- ..."
To find errors, search the "message" column for "ERROR" or "Exception".

## FAULT TYPE DETECTION GUIDE

Use this pattern to identify the fault type from the data:

| What you see in the data | Fault type |
|--------------------------|------------|
| `{service}_cpu` column spikes sharply | cpu stress |
| `{service}_mem` column grows steadily | memory stress |
| `lat_{service}_*` spikes but `_cpu`/`_mem` stay normal | network delay |
| `err_{service}_*` spikes toward 1.0 | packet loss |
| `{service}_diskio` column spikes sharply | disk I/O stress |
| socket metrics spike OR `err_` rises with normal CPU | socket exhaustion |

## NODE COLOCATION INFERENCE (from signal correlation)

Services running on the same Kubernetes node share CPU/memory resources.
When one service has a CPU/memory fault, colocated services are also starved.
This causes anomalies with NO trace call relationship between them.

Detect colocation by correlating CPU/memory time series:
```python
import re
# Get all CPU columns for services with anomalies
cpu_cols = [c for c in df.columns if c.endswith('_cpu')]
# Compute pairwise Pearson correlation
cpu_df = df[cpu_cols].corr()
# Services with correlation > 0.85 AND anomaly start within 60s → colocated
# Group into clusters using connected components
```

**TWO propagation mechanisms in the causal graph:**
1. **Node contention**: ROOT_CAUSE (cpu/mem fault) → colocated services starved (correlated signals)
2. **Trace cascade**: starved colocated service → its trace callers see latency/error spikes

## CLARIFICATION:
- System: Train Ticket booking platform (microservices).
- All timestamps are UTC. Convert Unix seconds → UTC with `pd.to_datetime(df['time'], unit='s', utc=True)`.
- Service names: ts-{name}-service (e.g., ts-auth-service, ts-order-service).

## ANOMALY DETECTION — REQUIRED VARIABLE NAMES

When performing Phase 1 anomaly detection, you MUST save results to these exact variable names
so that finalize_anomaly_queue() can read them directly from the kernel:

| Analysis type       | Variable name  | Required columns                                                  |
|---------------------|----------------|-------------------------------------------------------------------|
| CPU usage           | `anomaly_cpu`  | service, metric, score (float), earliest_time, duration_s (int)  |
| Memory usage        | `anomaly_mem`  | service, metric, score (float), earliest_time, duration_s (int)  |
| Trace latency       | `anomaly_lat`  | service, metric, score (float), earliest_time, duration_s (int)  |
| Trace error rate    | `anomaly_err`  | service, metric, score (float), earliest_time, duration_s (int)  |

- `service`: canonical service name (e.g. `ts-auth-service`)
- `metric`: the exact column name (e.g. `ts-auth-service_cpu`, `lat_ts-order-service_POST-order`)
- `score`: numeric anomaly magnitude (higher = more anomalous)
- `earliest_time`: UTC datetime string or pandas Timestamp
- `duration_s`: number of consecutive anomalous timestamps × 15 (seconds)
- **Only include rows where `duration_s >= 45`** (filters out brief spikes < 3 timestamps)

Example for CPU analysis (one row per metric column, duration-filtered):
```python
# For each _cpu column, compute score and duration
rows = []
for col in [c for c in df.columns if c.endswith('_cpu')]:
    svc = col.rsplit('_cpu', 1)[0]  # e.g. 'ts-auth-service'
    baseline = df[col][:20].mean()
    anomaly_mask = df[col] > baseline + 2 * df[col][:20].std()
    if not anomaly_mask.any():
        continue
    # Find longest consecutive run
    duration_s = int(anomaly_mask.astype(int).groupby(
        (~anomaly_mask).cumsum()).sum().max() * 15)
    if duration_s < 45:
        continue  # filter brief spikes
    score = float((df[col] - baseline).clip(lower=0).max())
    earliest = df.loc[anomaly_mask, 'time'].min()
    earliest_utc = pd.to_datetime(earliest, unit='s', utc=True)
    rows.append({'service': svc, 'metric': col, 'score': score,
                 'earliest_time': str(earliest_utc), 'duration_s': duration_s})
anomaly_cpu = pd.DataFrame(rows).sort_values('score', ascending=False)
print(anomaly_cpu)
```

You may also save any other useful DataFrames or variables — they persist in the
kernel across all execute() calls and can be re-used in later steps.
"""

# ---------------------------------------------------------------------------
# Token utilities
# ---------------------------------------------------------------------------

def count_message_tokens(message, enc):
    tokens = 4
    tokens += len(enc.encode(message.get("content", "")))
    return tokens


def trim_history_to_token_limit(history, max_tokens=120000, model="gpt-4"):
    enc = tiktoken.encoding_for_model(model)
    trimmed = []
    total_tokens = 0
    last_msg = history[-1]
    last_msg_tokens = count_message_tokens(last_msg, enc)

    if last_msg_tokens > max_tokens:
        truncated_content = enc.decode(enc.encode(last_msg["content"])[:max_tokens - 4])
        return [{"role": last_msg["role"], "content": truncated_content}]

    trimmed.insert(0, last_msg)
    total_tokens += last_msg_tokens

    for message in reversed(history[:-1]):
        message_tokens = count_message_tokens(message, enc)
        if total_tokens + message_tokens > max_tokens:
            break
        trimmed.insert(0, message)
        total_tokens += message_tokens

    return trimmed


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class Agent:
    def __init__(self):
        self.history = []
        self.llm = GPTClient(auth_type="azure_key")

    def get_model_name(self):
        return self.llm.get_model_name()

    def init_context(self, problem_desc: str, instructions: str, apis: dict):
        self.shell_api = self._filter_dict(apis, lambda k, _: "exec_shell" in k)
        self.submit_api = self._filter_dict(apis, lambda k, _: "submit" in k)
        self.telemetry_apis = self._filter_dict(
            apis, lambda k, _: "exec_shell" not in k and "submit" not in k
        )

        stringify_apis = lambda d: "\n\n".join(f"{k}{v}" for k, v in d.items())

        self.system_message = DOCS_TT_RCA.format(
            prob_desc=problem_desc,
            telemetry_apis=stringify_apis(self.telemetry_apis),
            shell_api=stringify_apis(self.shell_api),
            submit_api=stringify_apis(self.submit_api),
        )

        self.task_message = instructions
        self.history.append({"role": "system", "content": self.system_message})
        self.history.append({"role": "user",   "content": self.task_message})

    async def get_action(self, input) -> str:
        self.history.append({"role": "user", "content": self._add_instr(input)})
        trimmed_history = trim_history_to_token_limit(self.history)
        response = self.llm.run(trimmed_history)
        self.history.append({"role": "assistant", "content": response[0]})
        return response[0]

    def _filter_dict(self, dictionary, filter_func):
        return {k: v for k, v in dictionary.items() if filter_func(k, v)}

    def _add_instr(self, input):
        return input + "\n\n" + RESP_INSTR


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

SCORE_FIELDS = [
    "timestamp", "eval_id", "model", "problem_id",
    "task_type", "difficulty", "score", "success", "steps",
    "TTA", "in_tokens", "out_tokens",
    "fault_service", "fault_type",
    "correct_component", "correct_reason",
    "passing", "failing",
]

_FAULT_TYPE_TO_REASON = {
    "cpu":    "cpu stress",
    "mem":    "memory stress",
    "delay":  "network delay",
    "loss":   "packet loss",
    "disk":   "disk I/O stress",
    "socket": "socket exhaustion",
}


def append_score(scores_path: Path, eval_id: str, model: str, pid: str,
                 results: dict, fault_info: dict = None):
    is_new = not scores_path.exists()
    passing = results.get("passing_criteria", [])
    failing = results.get("failing_criteria", [])
    passing_str = " | ".join(passing) if passing else ""

    service = fault_info.get("service", "") if fault_info else ""
    fault   = fault_info.get("fault",   "") if fault_info else ""
    expected_reason = _FAULT_TYPE_TO_REASON.get(fault, fault)

    correct_component = service != "" and service in passing_str
    correct_reason    = expected_reason != "" and expected_reason in passing_str

    with open(scores_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SCORE_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        row = {
            "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "eval_id":     eval_id,
            "model":       model,
            "problem_id":  pid,
            "task_type":   results.get("task_type", "rca"),
            "difficulty":  results.get("difficulty", ""),
            "score":       results.get("score", ""),
            "success":     results.get("success", ""),
            "steps":       results.get("steps", ""),
            "TTA":         round(results.get("TTA", 0), 2),
            "in_tokens":   results.get("in_tokens", ""),
            "out_tokens":  results.get("out_tokens", ""),
            "fault_service":     service,
            "fault_type":        fault,
            "correct_component": correct_component,
            "correct_reason":    correct_reason,
            "passing":           passing_str,
            "failing":           " | ".join(failing) if failing else "",
        }
        writer.writerow(row)
