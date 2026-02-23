"""Agent-specific telemetry access guide for OpenRCA RCA Agent.

Injected into the task description's {telemetry_guide} placeholder.
Describes how the Controller-Executor architecture accesses telemetry data.
"""

_TELEMETRY_DESCRIPTIONS = {
    "log":    "Logs: system and application log records",
    "metric": "Metrics: time-series performance metrics",
    "trace":  "Traces: distributed tracing data",
}


def build_executor_telemetry_guide(enabled_types=None) -> str:
    """Build the executor telemetry guide based on which types are enabled.

    Args:
        enabled_types: frozenset of enabled type strings ("log", "metric", "trace"),
                       or None to include all three.
    """
    if enabled_types is None:
        types = list(_TELEMETRY_DESCRIPTIONS.keys())
    else:
        types = [t for t in ("log", "metric", "trace") if t in enabled_types]

    telemetry_lines = "\n".join(f"- {_TELEMETRY_DESCRIPTIONS[t]}" for t in types)

    return f"""\
How to analyze telemetry data:
You work with an Executor that writes and executes Python code to analyze telemetry data.
In each step, provide a clear, atomic instruction for the Executor.

The Executor can access:
{telemetry_lines}

Provide one instruction per step. The Executor will:
1. Generate Python code based on your instruction
2. Fetch and analyze the telemetry data

When you have completed your analysis, submit your findings:
```
submit({{"1": {{"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", "root cause component": "component_name", "root cause reason": "reason"}}}})
```"""


# Backward-compatible alias (all types enabled)
TELEMETRY_GUIDE = build_executor_telemetry_guide()
