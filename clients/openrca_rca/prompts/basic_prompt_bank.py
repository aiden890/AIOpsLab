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
- `apache*` — load balancer / entry point. Network faults (packet loss, latency) originate HERE and propagate downstream.
- `Mysql*`, `Redis*` — database / cache. Memory or connection faults originate HERE and propagate UPWARD to callers (Tomcat, IG, MG show JVM heap pressure as a downstream effect).
- `Tomcat*`, `IG*`, `MG*` — application services. JVM anomalies are OFTEN SYMPTOMS of an upstream network fault or downstream database fault, not the root cause.

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

**CRITICAL — resolve packet loss vs latency before saving hypothesis:**
- `get_trace_call_graph()` returning `network_gap` signal tells you a network fault exists, but NOT which type.
- Always resolve by checking the KPI direction for that component:
  - Run `get_component_kpi_deviation(namespace, component)` and look for NET* KPIs
  - NET* **drop below P10** → `"network packet loss"`
  - NET* **spike above P90** → `"network latency"`
- Never choose between packet loss and latency based on trace evidence alone.

**Apache and entry-point faults — how to identify:**
- `apache*` is the entry point of the call chain. A network fault at apache (e.g., packet loss on apache02) shows up as:
  - `get_kpi_low_deviation()`: NET* KPI drop **on apache02** itself ← PRIMARY signal
  - In traces: large elapsed time on components that CALL apache (Tomcat/IG/MG see the slow round-trip to apache); the trace call graph may rank Tomcat/IG as best candidates — but they are VICTIMS
- **Rule**: If `get_kpi_low_deviation()` shows NET* drop on apache01/02, apache IS the root cause — override any trace call graph suggestion.

**Database and cache faults (Redis*, Mysql*) — how to identify:**
- Redis/Mysql are LEAF nodes in the call chain — they do not call other services.
- Their faults show ONLY via KPI metrics. Trace callee signal for these components is always 0.
- To check: call `get_component_kpi_deviation(namespace, "Redis02")` and look for:
  - `used_memory` above P90 → `"high memory usage"` at that DB component
  - Do NOT skip database components just because the trace call graph does not rank them
- Database memory faults are often INDEPENDENT of network faults. Both can coexist:
  - Network fault on Tomcat/apache → service degradation
  - Redis memory spike → background cache pressure (may or may not be the root cause)
  - Distinguish by checking whether the DB anomaly coincides with the service fault window

**When scanning `get_kpi_high_deviation()` or `get_kpi_low_deviation()` results:**
- Always look for any row where `kpi` contains `NET` (e.g., `NETPackets*`, `NETKBTotal*`, `NETKBTotalPerSec`). These are the DIRECT network fault signals.
- Do not let JVM memory rows (which dominate the top of the list) distract you from NET* rows lower down — even a small NET* deviation is more diagnostically meaningful than a large JVM heap deviation.
- Use `execute()` with pandas to read the full deviation data and find NET* rows plus the exact `peak_high_ts` / `peak_low_ts`.
- **When multiple components appear in NET* rows, call `get_component_kpi_deviation()` for EACH of them** and compare their fault timestamps. The component with the **earliest** `peak_low_ts` or `peak_high_ts` is the root cause origin — faults propagate from origin to victims, so victims always show anomalies AFTER the root cause. Do not rely solely on the trace call graph to pick which NET* component to investigate.
- **Do NOT skip a component just because the trace call graph ranked a different one higher.** If Tomcat02 and Tomcat03 both show NET* drops, check both with `get_component_kpi_deviation()` before deciding.

**Victim vs root cause — the critical distinction:**
- If multiple components show anomalies, the root cause is the component whose anomaly **cannot be explained by what it calls**. Ask: "Is this component's anomaly caused by its own resource exhaustion, or by excessive calls/retries from its callers?"
- Apache CPU spike → caused by too many retries from clients = VICTIM. Apache NETPackets drop → actual packet loss = ROOT CAUSE.
- Mysql high Innodb writes → caused by retrying callers = VICTIM. Mysql high `used_memory` → actual memory fault = ROOT CAUSE.
- Tomcat JVM heap spike → caused by connection backlog to slow database = VICTIM. Tomcat `NETKBTotal` drop → actual network fault = ROOT CAUSE.

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
