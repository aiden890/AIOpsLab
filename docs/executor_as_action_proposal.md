# Proposal: Executor as a Static Action (ReAct RCA Agent)

## 1. Problem with Current Architectures

### `run_rca_agent.py` — Controller + Executor (current)
```
Controller LLM
  └── ALWAYS calls execute("instruction")
        └── Executor LLM → code → IPython → summarize
```
Every step costs 2–3 LLM calls, even for trivial queries that a pre-built
action already answers.

### `react_static.py` — ReAct (current)
```
ReAct LLM
  └── calls pre-built actions directly (get_log_overview, etc.)
        └── simple code runner (no Executor LLM, no self-correction)
```
`execute()` runs LLM-written code directly — no Executor LLM, no retry,
no summarization.

---

## 2. Proposed Architecture

```
ReAct RCA Agent (structured JSON response)
  │
  ├── {"thought": "Check for log anomalies first",
  │    "action": "get_log_overview", "args": {"namespace": "static-bank"}}
  │         → pre-built action, 0 extra LLM calls
  │
  ├── {"thought": "Need deeper analysis of Tomcat01 logs",
  │    "action": "execute",
  │    "args": {"instruction": "Load Tomcat01 logs, filter ERROR entries,
  │                             group by minute, find peak error period"}}
  │         → Executor LLM (max 3 retries) → code → IPython → summarize
  │
  ├── {"thought": "Correlate with metrics",
  │    "action": "get_anomaly_metrics", "args": {"namespace": "static-bank"}}
  │         → pre-built action, 0 extra LLM calls
  │
  └── {"thought": "Root cause identified",
       "action": "submit", "args": {"prediction": {"1": {...}}}}
```

**Key ideas:**
- Agent responds with **structured JSON** (reliable, no regex parsing)
- `execute()` is a **self-contained static action** (owns kernel + history + LLM)
- Agent chooses per step: cheap pre-built or powerful Executor
- Max **30 steps** per problem; `execute()` retries max **3 times** internally

---

## 3. Decisions

| Question | Decision |
|---|---|
| Agent response format | **Structured JSON** (recommended — reliable parsing) |
| Executor history across steps | **Persist** (recommended — IPython is stateful; reuse variables) |
| Supported task types | **All task types** (task_1 ~ task_7) |
| Executor max retries per `execute()` call | **3** |
| ReAct agent max steps per problem | **30** |
| `exec_shell` placement | Alongside pre-built actions |

---

## 4. New Files

### 4.1 `aiopslab/orchestrator/static_actions/rca_executor.py`

Inheritance chain:
```
StaticTaskActions
    └── StaticRCAActions          (submit + set_executor callback)
          └── StaticRCAActionsWithExecutor   ← NEW
```

`execute()` is **self-contained** — owns IPython kernel, executor history,
LLM config, and background schema. No injected callback needed.

```python
class StaticRCAActionsWithExecutor(StaticRCAActions):

    def setup_executor(
        self,
        background: str,        # domain schema for Executor LLM prompt
        api_config_path: str,   # path to api_config.yaml
        logger,                 # logging.Logger instance
    ):
        """Initialize IPython kernel, TelemetryHelper, and LLM config."""
        self._background = background
        self._configs = load_config(api_config_path)
        self._logger = logger
        self._executor_history = []          # persists across all execute() calls

        self._kernel = InteractiveShellEmbed()
        helper = TelemetryHelper(
            self,
            self._namespace,
            enable_log="log" in (self.enabled_telemetry_types or {"log"}),
            enable_metric="metric" in (self.enabled_telemetry_types or {"metric"}),
            enable_trace="trace" in (self.enabled_telemetry_types or {"trace"}),
        )
        self._kernel.push({"telemetry": helper})
        self._kernel.run_cell(
            "import pandas as pd\n"
            "pd.set_option('display.width', 427)\n"
            "pd.set_option('display.max_columns', 10)\n"
        )

    @executor_action
    def execute(self, instruction: str) -> str:
        """Generate and run Python code for custom telemetry analysis.

        The Executor LLM writes Python code from your instruction,
        runs it in a stateful IPython kernel, and returns a summarized result.
        Variables persist across calls — reuse them to avoid redundant fetches.
        Max 3 retries on execution error.
        """
        code, result, success, self._executor_history = execute_act(
            instruction=instruction,
            background=self._background,
            history=self._executor_history,   # stateful: persists across steps
            kernel=self._kernel,
            configs=self._configs,
            logger=self._logger,
            max_retries=3,                    # up from 2
        )
        return result

    def cleanup(self):
        """Release IPython kernel resources."""
        if hasattr(self, "_kernel"):
            self._kernel.reset()
```

**Lifecycle in runner:**
```python
actions = StaticRCAActionsWithExecutor(
    container_name=app.get_container_name(),
    possible_root_causes=dataset_config.get("possible_root_causes"),
    telemetry_flags=dataset_config.get("telemetry"),
    use_executor=dataset_config.get("executor", {}).get("enable", True),
)
actions.setup_executor(
    background=get_basic_prompt(dataset_key).schema,
    api_config_path=api_config_path,
    logger=logger,
)
# No agent.set_executor() call needed
```

### 4.2 `clients/openrca_rca/react_rca_agent.py`

ReAct-style agent with **structured JSON** responses. One LLM call per step.

**Structured JSON response format:**
```json
{
    "thought": "<analysis of the previous result and reasoning for next step>",
    "action": "<action_name>",
    "args": {
        "<param1>": "<value1>",
        "<param2>": "<value2>"
    }
}
```

**System prompt structure:**
```
SERVICE MONITORING TASK
{problem_desc}

## PRE-BUILT ANALYSIS ACTIONS (fast, no code needed):
{prebuilt_apis}
  ← get_log_overview, get_anomaly_metrics, get_trace_summary,
     get_metric_summary, search_logs, exec_shell

## EXECUTOR ACTION (for custom Python analysis):
{execute_api}
  ← execute(instruction): Executor LLM generates + runs Python code.
     Use when pre-built actions are insufficient.
     Variables persist in IPython across calls — reuse them.

## SUBMIT ACTION:
{submit_api}

{possible_root_causes}

At each turn, respond ONLY with a JSON object:
{
    "thought": "<your analysis>",
    "action": "<action_name>",
    "args": { ... }
}
```

**`init_context()` — splits APIs into sections:**
```python
def init_context(self, problem_desc, instructions, apis, possible_rca=None):
    self.execute_api   = {k: v for k, v in apis.items() if k == "execute"}
    self.shell_api     = {k: v for k, v in apis.items() if k == "exec_shell"}
    self.submit_api    = {k: v for k, v in apis.items() if k == "submit"}
    self.prebuilt_apis = {k: v for k, v in apis.items()
                          if k not in ("execute", "exec_shell", "submit")}
    # Build system prompt ...
```

If `executor.enable = false` in config → `execute` not in `apis` → not shown.

**`get_action()` — structured JSON in/out:**
```python
async def get_action(self, feedback: str) -> str:
    self.history.append({"role": "user", "content": feedback})
    response = self.llm.run(self.history)        # 1 LLM call
    self.history.append({"role": "assistant", "content": response})

    parsed = json.loads(extract_json(response))  # reliable, no regex
    action = parsed["action"]
    args   = parsed.get("args", {})

    # Convert to orchestrator action string
    return build_action_string(action, args)     # e.g. execute("...")
```

**Step limit:** 30 max steps per problem (passed to `orchestrator.start_problem`).

### 4.3 `clients/run_react_rca.py`

Runner combining the new agent + score CSV tracking:

```
python clients/run_react_rca.py --problem openrca_bank-task_1-0
python clients/run_react_rca.py --dataset openrca_bank
python clients/run_react_rca.py --dataset openrca_bank --task-type task_3
```

---

## 5. `executor.py` Change: max_retries Parameter

Current `execute_act()` hardcodes 2 attempts (`for attempt in range(2)`).
Add `max_retries=3` parameter:

```python
def execute_act(instruction, background, history, kernel, configs, logger,
                max_retries=3):
    for attempt in range(max_retries):
        ...
```

---

## 6. Comparison Table

| | `run_rca_agent.py` | `react_static.py` | **`run_react_rca.py`** |
|---|---|---|---|
| Agent style | Controller JSON | ReAct free-text | **ReAct structured JSON** |
| LLM calls / step | 2–3 | 1 | **1 (2–3 if execute() called)** |
| Pre-built actions | ✗ | ✓ | **✓** |
| Executor LLM | ✓ (every step) | ✗ | **✓ (on demand)** |
| Executor retries | 2 | — | **3** |
| Self-correction | ✓ | ✗ | **✓ (inside execute())** |
| Result summarization | ✓ | ✗ | **✓ (inside execute())** |
| Executor ownership | Agent callback | — | **Self-contained in action** |
| Executor history | Agent-owned | — | **Action-owned, persists** |
| Max steps / problem | 25 | 30 | **30** |
| Score CSV tracking | ✗ | ✓ | **✓** |
| All task types | ✓ | ✓ | **✓ (task_1 ~ task_7)** |

---

## 7. File Summary

| File | Type | Description |
|------|------|-------------|
| `aiopslab/orchestrator/static_actions/rca_executor.py` | **New** | `StaticRCAActionsWithExecutor` |
| `clients/openrca_rca/react_rca_agent.py` | **New** | Structured JSON ReAct agent |
| `clients/run_react_rca.py` | **New** | Runner with score CSV tracking |
| `clients/openrca_rca/executor.py` | **Modify** | Add `max_retries=3` parameter |
| `aiopslab/orchestrator/static_problems/openrca/base_task.py` | **Modify** | Use `StaticRCAActionsWithExecutor` |
