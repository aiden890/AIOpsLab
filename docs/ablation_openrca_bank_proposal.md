# Ablation Study Proposal: OpenRCA Bank – Telemetry × Executor

## Overview

This document proposes an ablation study on the **OpenRCA Bank** dataset to measure the isolated contribution of each telemetry modality (logs, traces, metrics) and the code-execution capability (Executor) on RCA accuracy.

---

## 1. Ablation Matrix

| Condition | Log | Trace | Metric | Executor | Label |
|-----------|:---:|:-----:|:------:|:--------:|-------|
| 1         | ✓   | ✓     | ✓      | ✗        | `all_telemetry` |
| 2         | ✓   | ✓     | ✓      | ✓        | `all_telemetry_exec` |
| 3         | ✓   | ✗     | ✗      | ✓        | `log_exec` |
| 4         | ✗   | ✓     | ✗      | ✓        | `trace_exec` |
| 5         | ✗   | ✗     | ✓      | ✓        | `metric_exec` |

**Research questions:**
- How much does the Executor (code-gen + IPython analysis) improve performance over direct pre-built actions? (Condition 1 vs. 2)
- Which single telemetry modality carries the most diagnostic signal? (Conditions 3, 4, 5 vs. 2)
- Are certain fault types (CPU, memory, network, disk, JVM) modality-dependent?

---

## 2. System Architecture Overview

```
run_ablation.py (new)
  │
  ├── AblationConfig (telemetry flags + executor flag)
  │     └── openrca_bank_{label}.json  (per-condition config)
  │
  ├── StaticOrchestrator
  │     └── StaticDataset.deploy()
  │           └── process_telemetry.py  ← respects enable_* flags
  │
  ├── StaticRCAActions
  │     ├── execute()          ← enabled only when executor=True
  │     ├── get_log_overview() ← always available (when log enabled)
  │     ├── get_metric_summary()
  │     ├── get_trace_summary()
  │     └── get_anomaly_metrics()
  │
  ├── OpenRCARCAAgent (modified)
  │     ├── executor mode   → IPython kernel + TelemetryHelper
  │     └── direct mode     → pre-built action calls only
  │
  └── TelemetryHelper (modified)
        └── Guards get_logs/metrics/traces by enabled flags
```

---

## 3. Config Schema Changes

### 3.1 Add `executor` block to JSON config

Add a new top-level `executor` block to `openrca_bank.json`:

```json
{
  "telemetry": {
    "enable_trace": true,
    "enable_log": true,
    "enable_metric": true
  },
  "executor": {
    "enable": true
  }
}
```

### 3.2 Per-Condition Config Files

Create five config files in `aiopslab/service/apps/static_dataset/config/`:

**`openrca_bank_all_telemetry.json`** (Condition 1)
```json
{
  "telemetry": { "enable_trace": true, "enable_log": true, "enable_metric": true },
  "executor":  { "enable": false }
}
```

**`openrca_bank_all_telemetry_exec.json`** (Condition 2)
```json
{
  "telemetry": { "enable_trace": true, "enable_log": true, "enable_metric": true },
  "executor":  { "enable": true }
}
```

**`openrca_bank_log_exec.json`** (Condition 3)
```json
{
  "telemetry": { "enable_trace": false, "enable_log": true, "enable_metric": false },
  "executor":  { "enable": true }
}
```

**`openrca_bank_trace_exec.json`** (Condition 4)
```json
{
  "telemetry": { "enable_trace": true, "enable_log": false, "enable_metric": false },
  "executor":  { "enable": true }
}
```

**`openrca_bank_metric_exec.json`** (Condition 5)
```json
{
  "telemetry": { "enable_trace": false, "enable_log": false, "enable_metric": true },
  "executor":  { "enable": true }
}
```

Each file is a **full copy** of `openrca_bank.json` with only `telemetry.*` and `executor.enable` overridden. All other fields (services, possible_root_causes, time_mapping, etc.) remain identical.

---

## 4. Implementation Plan

### 4.1 TelemetryHelper: Guard Disabled Types

**File:** `clients/openrca_rca/telemetry_helper.py`

Add telemetry-enable flags to `TelemetryHelper.__init__` and raise a clear error when disabled telemetry is accessed in executor code:

```python
class TelemetryHelper:
    def __init__(self, actions_obj, namespace,
                 enable_log=True, enable_metric=True, enable_trace=True):
        self._actions = actions_obj
        self._ns = namespace
        self._enable_log = enable_log
        self._enable_metric = enable_metric
        self._enable_trace = enable_trace

    def get_logs(self, service=None):
        if not self._enable_log:
            raise RuntimeError(
                "[Ablation] Logs are DISABLED in this configuration. "
                "Do not call telemetry.get_logs()."
            )
        ...

    def get_metrics(self, duration=5):
        if not self._enable_metric:
            raise RuntimeError(
                "[Ablation] Metrics are DISABLED in this configuration. "
                "Do not call telemetry.get_metrics()."
            )
        ...

    def get_traces(self, duration=5):
        if not self._enable_trace:
            raise RuntimeError(
                "[Ablation] Traces are DISABLED in this configuration. "
                "Do not call telemetry.get_traces()."
            )
        ...
```

The `RuntimeError` message is visible to the LLM executor result, so it serves both as a guard and as a natural-language hint that the executor code should avoid those methods.

### 4.2 Agent: Executor On/Off Flag

**File:** `clients/openrca_rca/agent.py`

Add `use_executor: bool` to `OpenRCARCAAgent.__init__` and branch in `set_actions`:

```python
class OpenRCARCAAgent:
    def __init__(self, api_config_path=None, use_executor=True):
        ...
        self.use_executor = use_executor

    def set_actions(self, actions_obj, namespace, dataset_key,
                    max_steps=25, telemetry_flags=None):
        ...
        if self.use_executor:
            # --- Current behavior: IPython kernel + TelemetryHelper ---
            flags = telemetry_flags or {}
            self.kernel = InteractiveShellEmbed()
            helper = TelemetryHelper(
                actions_obj, namespace,
                enable_log=flags.get("enable_log", True),
                enable_metric=flags.get("enable_metric", True),
                enable_trace=flags.get("enable_trace", True),
            )
            self.kernel.push({"telemetry": helper})
            ...
            actions_obj.set_executor(self._run_executor)
        else:
            # --- Direct-action mode: no IPython, no executor callback ---
            self.kernel = None
            # Do NOT call actions_obj.set_executor()
            # Controller will call pre-built actions directly
```

#### Direct-Action Mode: Controller Prompt

When `use_executor=False`, the controller system prompt changes to describe direct pre-built actions instead of the Executor pattern:

```
Available actions (call by name):
  get_log_overview(namespace)      → compact log anomaly summary
  get_anomaly_metrics(namespace)   → success rate / response-time anomalies
  get_metric_summary(namespace)    → per-service min/avg/max metrics
  get_trace_summary(namespace)     → per-service span statistics
  search_logs(namespace, keyword)  → keyword search over logs

Response format:
{
  "analysis": "...",
  "completed": "True" | "False",
  "action": "get_log_overview" | "get_anomaly_metrics" | ... | "submit",
  "args": { ... }
}
```

The controller returns a structured JSON that selects a direct action. `get_action()` translates this to the corresponding `perform_action()` call via the orchestrator. When `completed == "True"`, it falls through to `_generate_submit()` as before.

### 4.3 StaticRCAActions: Telemetry Availability Awareness

**File:** `aiopslab/orchestrator/static_actions/rca.py`

Pass telemetry flags through so the actions object knows which modalities are live. This prevents the agent from calling e.g. `get_trace_summary` when traces are disabled:

```python
class StaticRCAActions(StaticTaskActions):
    def __init__(self, *args, possible_root_causes=None,
                 telemetry_flags=None, **kwargs):
        super().__init__(*args, **kwargs)
        flags = telemetry_flags or {}
        self._enable_log = flags.get("enable_log", True)
        self._enable_metric = flags.get("enable_metric", True)
        self._enable_trace = flags.get("enable_trace", True)
        ...

    @action
    def get_log_overview(self, namespace: str) -> str:
        """Compact log anomaly summary."""
        if not self._enable_log:
            return "[Ablation] Log telemetry is disabled."
        return super().get_log_overview(namespace)

    @action
    def get_trace_summary(self, namespace: str, duration: int = 5) -> str:
        if not self._enable_trace:
            return "[Ablation] Trace telemetry is disabled."
        return super().get_trace_summary(namespace, duration)

    @action
    def get_metric_summary(self, namespace: str, duration: int = 5) -> str:
        if not self._enable_metric:
            return "[Ablation] Metric telemetry is disabled."
        return super().get_metric_summary(namespace, duration)
```

### 4.4 process_telemetry.py: Respect Telemetry Flags

**File:** `aiopslab-applications/static_dataset/process_telemetry.py`

The `config.json` already carries `enable_trace/log/metric`. Confirm that `mode_init` and `mode_stream` skip writing disabled telemetry types. Add explicit skip guards:

```python
def mode_init(config):
    telemetry = config.get("telemetry", {})
    for ttype, files in [
        ("log",    config["data_mapping"].get("log_files", [])),
        ("trace",  config["data_mapping"].get("trace_files", [])),
        ("metric", config["data_mapping"].get("metric_files", [])),
    ]:
        if not telemetry.get(f"enable_{ttype}", True):
            print(f"[process_telemetry] Skipping {ttype} (disabled by config)")
            continue
        # ... existing processing logic for this type
```

### 4.5 Ablation Runner

**File:** `clients/run_ablation.py` (new)

```python
"""Ablation runner: iterates over all 5 telemetry × executor conditions."""

ABLATION_CONDITIONS = [
    {
        "label": "all_telemetry",
        "config": "openrca_bank_all_telemetry",
        "use_executor": False,
    },
    {
        "label": "all_telemetry_exec",
        "config": "openrca_bank_all_telemetry_exec",
        "use_executor": True,
    },
    {
        "label": "log_exec",
        "config": "openrca_bank_log_exec",
        "use_executor": True,
    },
    {
        "label": "trace_exec",
        "config": "openrca_bank_trace_exec",
        "use_executor": True,
    },
    {
        "label": "metric_exec",
        "config": "openrca_bank_metric_exec",
        "use_executor": True,
    },
]
```

Usage:
```bash
# Run all 5 conditions on all Bank problems
python clients/run_ablation.py --dataset openrca_bank

# Run one condition only
python clients/run_ablation.py --condition all_telemetry_exec --dataset openrca_bank

# Run a single problem across all conditions
python clients/run_ablation.py --problem openrca_bank-task_1-0

# Override executor flag at CLI level (overrides config)
python clients/run_ablation.py --dataset openrca_bank --no-executor
```

Results are saved per condition: `results/ablation/openrca_bank/{label}/`.

---

## 5. Data Flow per Condition

### Condition 1 – All Telemetry, No Executor

```
Controller LLM
  ├── get_log_overview(ns)      → formatted text
  ├── get_anomaly_metrics(ns)   → formatted text
  ├── get_metric_summary(ns)    → DataFrame string
  ├── get_trace_summary(ns)     → DataFrame string
  └── submit({answer})
```

No IPython kernel is created. The controller directly selects pre-built analysis actions.

### Condition 2 – All Telemetry + Executor

```
Controller LLM
  └── execute("instruction")
        └── Executor LLM → Python code
              └── IPython kernel
                    ├── telemetry.get_logs()    → CSV path
                    ├── telemetry.get_metrics() → CSV path
                    └── telemetry.get_traces()  → CSV path
```

Current behavior, no changes.

### Conditions 3, 4, 5 – Single Modality + Executor

Same as Condition 2 but `TelemetryHelper` raises `RuntimeError` for disabled modalities:

```
telemetry.get_metrics()  # Condition 3: log_exec
  → RuntimeError: "[Ablation] Metrics are DISABLED ..."
  → Executor receives error → reports to Controller → Controller avoids metrics next step
```

---

## 6. File Change Summary

| File | Change Type | Description |
|------|------------|-------------|
| `aiopslab/service/apps/static_dataset/config/openrca_bank.json` | Modify | Add `executor.enable` field |
| `aiopslab/service/apps/static_dataset/config/openrca_bank_all_telemetry.json` | New | Condition 1 config |
| `aiopslab/service/apps/static_dataset/config/openrca_bank_all_telemetry_exec.json` | New | Condition 2 config |
| `aiopslab/service/apps/static_dataset/config/openrca_bank_log_exec.json` | New | Condition 3 config |
| `aiopslab/service/apps/static_dataset/config/openrca_bank_trace_exec.json` | New | Condition 4 config |
| `aiopslab/service/apps/static_dataset/config/openrca_bank_metric_exec.json` | New | Condition 5 config |
| `clients/openrca_rca/telemetry_helper.py` | Modify | Add enable flags + guards |
| `clients/openrca_rca/agent.py` | Modify | Add `use_executor` flag, direct-action mode |
| `aiopslab/orchestrator/static_actions/rca.py` | Modify | Pass telemetry flags, guard disabled actions |
| `aiopslab-applications/static_dataset/process_telemetry.py` | Modify | Skip disabled telemetry types explicitly |
| `clients/run_rca_agent.py` | Modify | Accept `--use-executor / --no-executor` CLI flag |
| `clients/run_ablation.py` | New | Ablation runner over all 5 conditions |

---

## 7. Open Questions Before Implementation

1. **Direct-action mode response format**: Should the controller output a structured JSON like `{ "action": "get_log_overview", "args": {} }`, or should it output a natural-language command that gets parsed into an action call? The structured JSON approach is more reliable.

2. **Executor telemetry guide**: When only one modality is available, the `TELEMETRY_GUIDE` injected into the controller system prompt should be filtered to only describe the enabled modality. Should this be done by template substitution, or by maintaining separate prompt files per modality?

3. **Task types for ablation**: All 7 task types (`task_1` through `task_7`) or a representative subset? Some task types may be more log-centric or metric-centric, affecting fair comparison.

4. **Repetitions**: How many runs per condition per problem for statistical reliability?
