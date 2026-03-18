# Static Actions Improvement Plan for RCA Quality

## TL;DR

The current `StaticTaskActions` in `base.py` exposes raw, unfiltered telemetry that overwhelms the agent.
Three root problems drive the low RCA score:

1. **No data retrieved** — Docker container path mismatch or not running, agent immediately fails
2. **No time-window filtering** — `get_logs` returns ALL data (1.18M rows for one day) with no way to focus on the fault window
3. **No analytical actions** — agent must read raw CSV dumps with no summarization or anomaly detection

---

## Current Action Inventory

| Action | What it does | Problems |
|---|---|---|
| `get_logs(namespace, service?)` | Fetches ALL logs → saves to `static_logs_output/logs.csv` → returns path | No time filter; 1.18M rows/day when it works |
| `get_metrics(namespace, duration?)` | Fetches last N minutes of metrics → saves CSV | `duration` uses `datetime.now()` — never matches 2021 timestamps |
| `get_traces(namespace, duration?)` | Fetches last N minutes of traces → saves CSV | Same timestamp bug; 12.15M rows/day |
| `read_logs(file_path)` | Dumps entire CSV as string | Context window killer on large files |
| `read_metrics(file_path)` | Dumps entire CSV as string | Same issue |
| `read_traces(file_path)` | Dumps entire CSV as string | Same issue |
| `exec_shell(command)` | Runs restricted shell commands | Only useful AFTER `get_*` creates output dirs; doesn't help with no data |

---

## Observed Failure Mode (from log trace)

```
Step 1: get_logs("static-bank")          → "No logs found"
Step 2: get_metrics("static-bank", 30)   → "No metrics found"
Step 3: get_traces("static-bank", 30)    → "No traces found"
Step 4: get_logs("static-dataset")       → "No logs found"   (wrong namespace)
Step 5: exec_shell("ls static_logs_output")  → directory not found
Step 6-7: same for metrics/traces dirs
Step 8: exec_shell("grep -r 'error' /")  → access denied
Step 9: submit(...unknown...)            → score: 0.0
```

**Root cause of failure**: The Docker container with processed telemetry data was not running (or the path `/agent/telemetry/static-bank/logs` didn't exist). After the first failure, the agent had no recovery path — no fallback, no discovery mechanism.

---

## Data Schema Reference

### Bank Dataset (`static-bank` namespace)

**Logs** (`log_service.csv`): 1,182,984 rows for 2021-03-04
```
log_id | timestamp (unix) | cmdb_id | log_name | value
```
- `cmdb_id` (service): Tomcat01-04, apache01-02
- `log_name`: apache_access_log, catalina, gc, localhost, localhost_access_log
- Time range: 16:00 UTC prev day → 11:06 UTC next day (= 00:00-19:06 CST)

**Metrics** (`metric_app.csv`): 13,568 rows for 2021-03-04
```
timestamp | rr (request rate) | sr (success rate) | cnt | mrt (mean response time) | tc (service)
```
- Services: ServiceTest1-N

**Traces** (`trace_span.csv`): 12,152,300 rows for 2021-03-04
```
timestamp | cmdb_id | parent_id | span_id | trace_id | duration
```

### Task 1 Ground Truth
- Query window: 2021-03-04 14:30-15:00 (CST) = 06:30-07:00 UTC
- Answer: `2021-03-04 14:57:00` (CST)
- Key signal: anomaly in logs/metrics around 14:57 CST

---

## Identified Problems

### P1: Time-filtering Bug in `get_metrics` / `get_traces`
```python
# In static_app.py _filter_by_time():
now = datetime.now().timestamp()  # 2026 timestamp!
cutoff = now - (duration_minutes * 60)
filtered = df[df["timestamp"] >= cutoff]  # No 2021 data matches
# Falls back to all data — "works" accidentally, but wrong intent
```

### P2: `get_logs` Has No Time Filtering At All
Agent is told "find the fault between 14:30-15:00" but can only get ALL 1.18M log rows.

### P3: `read_*` Dumps Entire File to Context Window
A 1.18M row log file dumped as string = millions of tokens. Agent can't analyze it.

### P4: No Discovery Mechanism
When data is missing, the agent has no way to check:
- What services are available?
- What time range does the data cover?
- Is any telemetry available at all?

### P5: No Anomaly-Focused Actions
The agent must reason about root cause, but can only dump raw data. No actions to:
- Find log lines with errors/exceptions
- Find services with degraded metrics
- Find slow/failed traces

---

## Proposed New Action List

### Priority 1 — Critical (fixes empty data & size problem)

#### `get_log_overview(namespace: str) → str`
**Purpose**: Fast first-look. Returns summary stats without raw data.
**Output**:
```
Log data overview for namespace 'static-bank':
  Total rows: 1,182,984
  Time range: 2021-03-03 16:00:00 UTC → 2021-03-04 11:06:31 UTC
  Services (cmdb_id):
    Tomcat01: 320,450 rows
    Tomcat02: 298,120 rows
    ...
  Log types (log_name):
    gc: 540,000 rows
    catalina: 280,000 rows
    ...
```
**Why**: Agent knows what exists before fetching anything. Zero context waste.

---

#### `get_logs_window(namespace: str, start_time: str, end_time: str, service: str = None) → str`
**Purpose**: Fetch logs for a specific time window (ISO datetime strings in CST/local timezone).
**Example**: `get_logs_window("static-bank", "2021-03-04 14:30:00", "2021-03-04 15:00:00")`
**Output**: Path to CSV with only matching rows (~1,000-5,000 rows instead of 1.18M)
**Why**: Agent gets exactly the fault window data, not all history.

---

#### `search_logs(namespace: str, keyword: str, start_time: str = None, end_time: str = None, limit: int = 100) → str`
**Purpose**: Grep-like search in log `value` field, with optional time window.
**Example**: `search_logs("static-bank", "Exception", "2021-03-04 14:00:00", "2021-03-04 15:30:00")`
**Output**: Top `limit` matching rows as formatted table
**Why**: Agent can directly search for error patterns instead of reading entire file.

---

### Priority 2 — High Impact (anomaly detection)

#### `get_anomaly_metrics(namespace: str, start_time: str, end_time: str) → str`
**Purpose**: Returns services with degraded performance (low success rate or high response time).
**Output**:
```
Anomalous metrics in 'static-bank' from 14:30 to 15:00:
  Service     | Period Min SR | Normal Avg SR | Deviation
  Tomcat01    | 65.2%         | 99.8%         | -34.6% *** ANOMALY
  ServiceTest3| 100.0%        | 100.0%        | normal
```
**Why**: Directly surfaces which service degraded — key for RCA.

---

#### `get_metric_summary(namespace: str, start_time: str = None, end_time: str = None) → str`
**Purpose**: Per-service aggregated metrics (min/max/avg of rr, sr, mrt) for a window.
**Output**: Compact table instead of raw rows.
**Why**: 13.5K metric rows summarized into ~20 service rows.

---

#### `get_trace_summary(namespace: str, start_time: str, end_time: str) → str`
**Purpose**: Aggregate trace stats without dumping 12M rows.
**Output**:
```
Trace summary in 'static-bank' from 14:30 to 15:00:
  Service   | Span Count | Avg Duration | Max Duration | Error Spans
  dockerA2  | 45,230     | 25ms         | 8,420ms      | 12 (0.03%)
  Tomcat01  | 1,204      | 550ms        | 4,100ms      | 98 (8.1%) ***
```
**Why**: Agent gets anomaly signals without consuming full trace data.

---

### Priority 3 — Quality of Life

#### `read_logs(file_path: str, limit: int = 200, offset: int = 0) → str`
**Change**: Add `limit` and `offset` to existing `read_logs/metrics/traces`.
**Why**: Prevents context window overflow when agent does fetch raw data.

#### `get_services(namespace: str) → str`
**Purpose**: List available services in the dataset.
**Output**: `Services: Tomcat01, Tomcat02, apache01, apache02, ...`
**Why**: Agent knows valid service names before calling other actions.

---

## Files to Modify

```
aiopslab/orchestrator/static_actions/base.py   ← Main changes (new actions)
aiopslab/service/static_app.py                 ← Add time-filtered fetch methods
```

## Files NOT to Modify
- `rca.py` — only adds `submit()`; inherits all base actions
- `static_orchestrator.py` — no changes needed
- `clients/react_static.py` — no changes needed (actions auto-expose)

---

## Implementation Sequence

1. **Add `_parse_time()` helper** to `StaticTaskActions` — converts ISO string to unix timestamp
2. **Add `get_log_overview()`** — fast summary from Docker, no file write needed
3. **Modify `get_logs()`** — add `start_time`/`end_time` params (ISO strings)
4. **Add `search_logs()`** — keyword search with optional time window
5. **Fix `get_metrics()` / `get_traces()`** — replace wall-clock filter with explicit time window params
6. **Add `get_anomaly_metrics()`** — compute degradation vs. baseline
7. **Add `get_metric_summary()`** — aggregated stats per service
8. **Add `get_trace_summary()`** — aggregated trace stats
9. **Add `limit`/`offset` to `read_*`** — safe pagination

---

## Expected Agent Flow After Improvements

```
Step 1: get_log_overview("static-bank")
        → See: data exists, time range, services list

Step 2: get_anomaly_metrics("static-bank", "2021-03-04 14:30:00", "2021-03-04 15:00:00")
        → See: Tomcat01 success rate dropped at 14:57

Step 3: search_logs("static-bank", "ERROR", "2021-03-04 14:50:00", "2021-03-04 15:00:00")
        → See: exception logs at 14:57 from Tomcat01

Step 4: submit({"1": {"root cause occurrence datetime": "2021-03-04 14:57:00", ...}})
        → Score: 1.0
```
