# Metric RAG Method for Executor

## Problem

The KG RCA agent's executor (gpt-4.1-mini) frequently generates code that references KPIs that don't exist on the target component. For example, it searches for `connected_clients` on `db_007` when that KPI only exists on `redis_*` components. The static background prompt provides only schema examples, not actual per-component KPI inventories.

## Solution: Pre-computed Metric Index with RAG Injection

We pre-compute a **Metric Knowledge Graph** (`MetricKG`) from actual telemetry data at problem init time, then inject relevant component KPI info into each `execute()` call via regex-based component extraction.

```
┌─────────────────────┐
│  Problem Init       │
│                     │
│  metric_df ─┐       │
│  trace_df ──┤       │
│             ▼       │
│    build_metric_kg()│
│         │           │
│         ▼           │
│     MetricKG        │
│  (component_profiles│
│   trace_profiles    │
│   app_services)     │
└────────┬────────────┘
         │
         │ set_rag_injector(closure)
         ▼
┌─────────────────────────────────────────┐
│  Each execute() call                    │
│                                         │
│  instruction ──► extract_components()   │
│                        │                │
│                        ▼                │
│               format_rag_context()      │
│                        │                │
│                        ▼                │
│            enriched instruction ──► LLM │
└─────────────────────────────────────────┘
```

## Architecture

### Module: `aiopslab/orchestrator/static_actions/kg/metric_kg_builder.py`

Follows the same structural pattern as `trace_kg_builder.py`.

### Data Structures

#### `KPIStats`

Per-KPI statistics on a single component.

| Field   | Type    | Description                          |
|---------|---------|--------------------------------------|
| `name`  | `str`   | KPI name (e.g. `CPU_Used_Pct`)      |
| `count` | `int`   | Number of data points                |
| `mean`  | `float` | Mean value                           |
| `std`   | `float` | Standard deviation                   |
| `min`   | `float` | Minimum value                        |
| `max`   | `float` | Maximum value                        |
| `p50`   | `float` | 50th percentile (median)             |
| `p95`   | `float` | 95th percentile                      |

#### `ComponentMetricProfile`

Per-component profile containing all available KPIs.

| Field            | Type              | Description                                      |
|------------------|-------------------|--------------------------------------------------|
| `cmdb_id`        | `str`             | Component ID (e.g. `db_007`, `Tomcat01`)         |
| `component_type` | `str`             | Type prefix (e.g. `db`, `redis`, `Tomcat`)       |
| `kpi_stats`      | `list[KPIStats]`  | All KPIs available on this component (sorted)    |
| `fault_kpis`     | `list[str]`       | KPIs matching fault-relevant patterns            |

#### `TraceProfile`

Per-callee trace summary (aggregated from trace data).

| Field          | Type    | Description                          |
|----------------|---------|--------------------------------------|
| `callee`       | `str`   | Callee component name                |
| `total_calls`  | `int`   | Total trace calls to this component  |
| `fail_count`   | `int`   | Number of failed calls               |
| `fail_rate`    | `float` | Failure ratio (0.0 - 1.0)           |
| `elapsed_mean` | `float` | Mean elapsed time (ms)               |
| `elapsed_p95`  | `float` | 95th percentile elapsed time (ms)    |

#### `MetricKG`

Top-level container (analogous to `TraceKG`).

| Field                | Type                                      | Description                                |
|----------------------|-------------------------------------------|--------------------------------------------|
| `component_profiles` | `dict[str, ComponentMetricProfile]`       | Keyed by `cmdb_id`                         |
| `trace_profiles`     | `dict[str, TraceProfile]`                 | Keyed by callee name                       |
| `app_metric_services`| `list[str]`                               | App-level services (`osb_*`, `ServiceTest*`)|
| `dataset_type`       | `str`                                     | `"bank"` or `"telecom"`                    |
| `total_metric_rows`  | `int`                                     | Total rows in source metric DataFrame      |
| `total_trace_rows`   | `int`                                     | Total rows in source trace DataFrame       |

### Functions

#### `build_metric_kg(metric_df, trace_df=None, dataset_type=None) -> MetricKG`

Main builder. Groups metrics by `cmdb_id`, computes per-KPI statistics, identifies fault-relevant KPIs, and optionally adds per-callee trace summaries.

- **Schema handling**: Auto-detects Telecom (`name` column) vs Bank (`kpi_name` column)
- **App metrics**: Identified separately via `serviceName` (Telecom) or `tc` (Bank)
- **Trace profiles**: Built from `dsName` (Telecom) or `cmdb_id` (Bank) grouping

#### `extract_components_from_instruction(instruction, dataset_type) -> list[str]`

Regex-based extraction of component IDs from natural language instructions.

| Dataset  | Pattern                                                              |
|----------|----------------------------------------------------------------------|
| Telecom  | `os_\d+`, `docker_\d+`, `db_\d+`, `redis_\d+`, `osb_\d+`          |
| Bank     | `Tomcat\d+`, `Mysql\d+`, `Redis\d+`, `apache\d+`, `MG\d+`, `IG\d+`, `dockerA\d+`, `dockerB\d+`, `dockerC\d+`, `ServiceTest\d+` |

Returns de-duplicated list preserving order. Returns `[]` if no components found.

#### `format_rag_context(kg, components) -> str`

Formats component profiles into a text block for LLM prompt injection. Returns `""` if no matching profiles exist (executor falls back to static schema).

### Fault KPI Patterns (`FAULT_KPI_PATTERNS`)

Maps fault reasons to regex patterns that identify relevant KPI names.

| Fault Reason            | Patterns                                             |
|-------------------------|------------------------------------------------------|
| CPU fault               | `cpu`, `iowait`, `container_cpu`                     |
| network delay           | `net`, `traffic`, `packets`                          |
| network loss            | `drop`, `error.*packet`                              |
| db connection limit     | `connected_client`, `Sess_Connect`                   |
| db close                | `On_Off_State`, `tnsping`                            |
| high CPU usage          | `cpu`, `CPUCpuUtil`                                  |
| high memory usage       | `mem`, `MemUtil`                                     |
| high disk I/O read usage| `disk`, `io`                                         |
| high disk space usage   | `disk`, `FileSystem`                                 |
| high JVM CPU load       | `jvm`, `cpu`                                         |
| JVM OOM Heap            | `jvm`, `heap`, `mem`                                 |

## Integration Points

### 1. `executor/actions.py` (minimal changes)

```python
# __init__: new field
self._rag_injector = None

# New method
def set_rag_injector(self, injector):
    """Store a Callable[[str], str] that transforms instructions."""
    self._rag_injector = injector

# execute(): before execute_act() call
if self._rag_injector is not None:
    instruction = self._rag_injector(instruction)
```

### 2. `clients/run_kg_rca.py` (wiring)

After `setup_executor()`, ~20 lines:

```python
metric_df = actions.static_app.fetch_metrics_df(namespace, ...)
trace_df = actions.static_app.fetch_traces_df(namespace, ...)
metric_kg = build_metric_kg(metric_df, trace_df, dataset_type=...)

def _rag_injector(instruction, _kg=metric_kg, _dt=dataset_type):
    comps = extract_components_from_instruction(instruction, _dt)
    if not comps:
        return instruction
    ctx = format_rag_context(_kg, comps)
    if not ctx:
        return instruction
    return f"{ctx}\n\n{instruction}"

actions.set_rag_injector(_rag_injector)
```

## Output Format

When an executor instruction mentions `db_007`, the injected context looks like:

```
=== AVAILABLE METRIC DATA FOR REFERENCED COMPONENTS ===

[db_007] (type: db)
  Available KPIs (46 total):
    - CPU_Used_Pct: mean=18.5, std=4.2, p50=17.2, p95=28.5 (n=1440)
    - MEM_real_util: mean=57.3, std=1.2, p50=57.1, p95=59.8 (n=1440)
    - Sess_Connect: mean=396.0, std=12.5, p50=395, p95=415 (n=1440)
    ...
  Fault-relevant KPIs: CPU_Used_Pct, MEM_real_util, Sess_Connect, On_Off_State
  Trace calls (as callee): total=4800, fails=120, fail_rate=0.025,
    mean_elapsed=15.3ms, p95_elapsed=450.2ms

=== END METRIC DATA ===

<original instruction here>
```

When no components are mentioned in the instruction, RAG injection is skipped entirely and the executor uses only the existing static schema from its background prompt.

## Files Changed

| File | Action | Lines |
|------|--------|-------|
| `aiopslab/orchestrator/static_actions/kg/metric_kg_builder.py` | Created | ~280 |
| `aiopslab/orchestrator/static_actions/kg/__init__.py` | Modified | +4 |
| `aiopslab/orchestrator/static_actions/executor/actions.py` | Modified | +12 |
| `clients/run_kg_rca.py` | Modified | +22 |

## Verification

```bash
# Single problem — check RAG context in executor logs
python clients/run_kg_rca.py --problem openrca_telecom-task_2-2 --max-steps 10

# Full evaluation
python clients/run_kg_rca.py --dataset openrca_telecom --eval-id with-rag
```
