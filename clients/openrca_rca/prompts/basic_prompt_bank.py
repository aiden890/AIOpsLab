cand = """## POSSIBLE ROOT CAUSE REASONS:

- high CPU usage
- high memory usage
- network latency
- network packet loss
- high disk I/O read usage
- high disk space usage
- high JVM CPU load
- JVM Out of Memory (OOM) Heap

## POSSIBLE ROOT CAUSE COMPONENTS:

- apache01
- apache02
- Tomcat01
- Tomcat02
- Tomcat04
- Tomcat03
- MG01
- MG02
- IG01
- IG02
- Mysql01
- Mysql02
- Redis01
- Redis02"""

schema = f"""## TELEMETRY DATA ACCESS:

- Use `telemetry.get_logs()`, `telemetry.get_metrics()`, `telemetry.get_traces()` to fetch data.
- Each returns full file path. Read CSVs from there (e.g., `pd.read_csv(f"{{metrics_path}}")`).

## DATA SCHEMA

1.  **Metric columns** (in metrics.csv):

    - Container metrics:
        ```csv
        timestamp,cmdb_id,kpi_name,value
        1614787200,Tomcat04,OSLinux-CPU_CPU_CPUCpuUtil,26.2957
        ```

    - App metrics:
        ```csv
        timestamp,rr,sr,cnt,mrt,tc
        1614787440,100.0,100.0,22,53.27,ServiceTest1
        ```

2.  **Trace columns** (in traces.csv):

    ```csv
    timestamp,cmdb_id,parent_id,span_id,trace_id,duration
    1614787199,dockerA2,369-bcou-dle-way1-c514cf30-43410@0824-2f0e47a816-17492,21030300016145905763,gw0120210304000517192504,19
    ```

3.  **Log columns** (in logs.csv):

    ```csv
    log_id,timestamp,cmdb_id,log_name,value
    8c7f5908ed126abdd0de6dbdd739715c,1614787201,Tomcat01,gc,"3748789.580: [GC (CMS Initial Mark) ..."
    ```

{cand}

## CLARIFICATION OF TELEMETRY DATA:

1. This microservice system is a banking platform.

2. The app metrics only contain four KPIs: rr, sr, cnt, and mrt. In contrast, container metrics record a variety of KPIs such as CPU usage and memory usage. The specific names of these KPIs can be found in the `kpi_name` field.

3. In different telemetry files, the timestamp units are all in seconds.
"""

guidance = """\
## BANK-SPECIFIC RCA GUIDANCE:

**Component types and fault propagation direction:**
- `apache*` — load balancer / entry point.
- `Mysql*`, `Redis*` — database / cache.
- `Tomcat*`, `IG*`, `MG*` — application services.

**KPI signal → reason mapping (use this table to select the exact reason string):**
| KPI pattern | Direction | Reason to use |
|---|---|---|
| `NETKBTotalPerSec`, `NETPackets*` | drop **below P10** (`get_kpi_low_deviation`) | `"network packet loss"` |
| `NETKBTotalPerSec`, `NETPackets*` | spike **above P90** (`get_kpi_high_deviation`) | `"network latency"` |
| `used_memory`, `JVMUsedMemory`, `HeapMemoryUsed` | above P90 | `"high memory usage"` |
| `CPUCpuUtil` | above P90 | `"high CPU usage"` |
| `DSKRead`, disk KPIs | above P90 | `"high disk I/O read usage"` |
| disk space KPIs | above P90 | `"high disk space usage"` |
| `JVMCpuLoad` | above P90 | `"high JVM CPU load"` |
| logs show `java.lang.OutOfMemoryError` or heap > 95% | — | `"JVM Out of Memory (OOM) Heap"` |

**Victim vs root cause — the critical distinction:**
- If multiple components show anomalies, the root cause is the component whose anomaly **cannot be explained by what it calls**. Ask: "Is this component's anomaly caused by its own resource exhaustion, or by excessive calls/retries from its callers?"
- Apache CPU spike → caused by too many retries from clients = VICTIM. Apache NETPackets drop → actual packet loss = ROOT CAUSE.
- Mysql high Innodb writes → caused by retrying callers = VICTIM. Mysql high `used_memory` → actual memory fault = ROOT CAUSE.
- Tomcat JVM heap spike → caused by connection backlog to slow database = VICTIM. Tomcat `NETKBTotal` drop → actual network fault = ROOT CAUSE.

**JVM Out of Memory (OOM) — how to identify:**
- **KEY SIGNAL — heap oscillation**: When `get_kpi_high_deviation()` AND `get_kpi_low_deviation()` **both flag the same JVM memory KPI** (`HeapMemoryUsed` or `JVMUsedMemory`) for the same component, the heap is cycling between exhausted (after GC frees it, value drops below P10) and full again (before next GC, value rises above P90). This is the definitive JVM OOM pattern.
- When you see this pattern, **immediately search logs**: `search_logs(namespace, keyword="OutOfMemoryError")` — search by keyword, NOT by component name.
- **CRITICAL — trace `network_gap` is caused by GC pauses, not network**: During a JVM OOM event, Stop-The-World GC pauses freeze all threads. This makes parent span duration >> sum(child span durations), which the trace analysis reports as a `network_gap` signal. **Do NOT interpret trace `network_gap` as network packet loss when JVM heap oscillation is the dominant anomaly** — they produce identical trace signals but completely different root causes.

**CPU exhaustion also causes trace `network_gap` — do NOT confuse with network faults:**
- When a component's `CPUCpuUtil` spikes significantly above P90, its threads are saturated. Slow thread processing creates parent-span duration >> sum(child-span durations), which appears as `network_gap` in trace analysis — but it is CPU starvation, not network delay.
- **Rule**: If `CPUCpuUtil` is significantly above P90 for a component AND the trace shows `network_gap` → reason = `"high CPU usage"`, NOT network packet loss or latency.
- `CPUCpuUtil` deviation takes strict priority over trace `network_gap` signal when both appear together.

**`NETPacketsIn` / `NETPacketsOut` HIGH deviation is often a false signal:**
- These are **cumulative counters** that grow monotonically all day. Their P90 threshold is the 90th percentile of cumulative daily values — slightly exceeding it happens naturally and does NOT indicate network latency.
- Do NOT use `NETPacketsIn/Out` HIGH deviation alone to conclude `"network latency"`. Use `NETKBTotalPerSec` (a rate KPI) instead — that is the reliable network throughput signal.

**JVM memory vs real memory fault:**
- High `JVMFreeMemory` = GC just ran and freed memory — this is NORMAL behavior, not a fault signal.
- High `JVMUsedMemory` or `HeapMemoryUsed` = heap pressure — may be caused by database connection backlog, not the root cause itself.
- `Allocation Failure` in GC logs = routine minor GC — NOT evidence of OOM. Only use `JVM Out of Memory (OOM) Heap` as the reason if logs show `java.lang.OutOfMemoryError` or heap usage is sustained above 95%.

**Trace signal direction — caller_score vs callee_score:**
- `callee_score > 0`: the component IS failing when called as a server → this IS a fault origin signal.
- `caller_score` high, `callee_score = 0.00`: the component is slow AS A CLIENT (victim of its callees) → NOT the root cause.
- In the Bank dataset, `callee_score` is almost always 0. Therefore `get_trace_call_graph()` alone is NOT sufficient — always use metric KPI patterns above to identify the root cause.

**When trace analysis is inconclusive:**
- If `get_trace_call_graph()` returns all top candidates with `combined ≈ 1.0` and `callee_score = 0.00`, the trace has no failure signal — do NOT use trace ranking to choose the component. Fall back to the KPI patterns above.
"""
