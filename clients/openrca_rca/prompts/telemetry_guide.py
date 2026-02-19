"""Agent-specific telemetry access guide for OpenRCA RCA Agent.

Injected into the task description's {telemetry_guide} placeholder.
Describes how the Controller-Executor architecture accesses telemetry data.
"""

TELEMETRY_GUIDE = """\
How to analyze telemetry data:
You work with an Executor that writes and executes Python code to analyze telemetry data.
In each step, provide a clear, atomic instruction for the Executor.

The Executor can access:
- Logs: system and application log records
- Metrics: time-series performance metrics
- Traces: distributed tracing data

Provide one instruction per step. The Executor will:
1. Generate Python code based on your instruction
2. Fetch and analyze the telemetry data
3. Return a summary of the results

When you have completed your analysis, submit your findings:
```
submit({{"1": {{"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", "root cause component": "component_name", "root cause reason": "reason"}}}})
```"""
