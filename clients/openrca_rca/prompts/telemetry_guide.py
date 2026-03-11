"""Agent-specific telemetry access guide for OpenRCA RCA Agent.

Injected into the task description's {telemetry_guide} placeholder.
Describes API-based telemetry access and analysis guidance.
"""

_TELEMETRY_DESCRIPTIONS = {
    "log":    "Logs: system and application log records",
    "metric": "Metrics: time-series performance metrics",
    "trace":  "Traces: distributed tracing data",
}

_EXECUTOR_ACTION_DESCRIPTIONS = {
    "execute": (
        "execute(instruction: str) -> str "
        "(returns summarized answer and includes raw output section)"
    ),
    "execute_anomaly_report": (
        "execute_anomaly_report(instruction: str) -> str "
        "(returns structured anomaly report JSON with fields like window_utc, "
        "kpi_results, sustained_windows, target_timestamp_check; no raw output section)"
    ),
}


def build_executor_telemetry_guide(enabled_types=None, executor_actions=None) -> str:
    """Build the executor telemetry guide based on which types are enabled.

    Args:
        enabled_types: frozenset of enabled type strings ("log", "metric", "trace"),
                       or None to include all three.
        executor_actions: list of enabled executor API names (e.g. ["execute"] or
                          ["execute_anomaly_report"]). Defaults to ["execute"].
    """
    if enabled_types is None:
        types = list(_TELEMETRY_DESCRIPTIONS.keys())
    else:
        types = [t for t in ("log", "metric", "trace") if t in enabled_types]

    telemetry_lines = "\n".join(f"- {_TELEMETRY_DESCRIPTIONS[t]}" for t in types)
    action_names = executor_actions or ["execute"]
    action_lines = "\n".join(
        f"- {_EXECUTOR_ACTION_DESCRIPTIONS[name]}"
        for name in action_names
        if name in _EXECUTOR_ACTION_DESCRIPTIONS
    )
    if not action_lines:
        action_lines = f"- {_EXECUTOR_ACTION_DESCRIPTIONS['execute']}"

    return f"""\
How to analyze telemetry data:
Use the available APIs to retrieve and analyze telemetry data.
In each step, provide one clear, atomic API call.

Available telemetry data types for this run:
{telemetry_lines}

Available analysis APIs for this run:
{action_lines}

Guidelines:
1. Use one API call per response.
2. Reuse cached results/variables when possible to avoid redundant work.
3. Use UTC timestamps consistently.

When you have completed your analysis, submit your findings:
```
submit({{"1": {{"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", "root cause component": "component_name", "root cause reason": "reason"}}}})
```"""


def build_telemetry_guide(condition="all", dataset_key=None) -> str:
    """Build telemetry guide filtered by ablation condition and dataset.

    Args:
        condition: "all", "no_log", "no_metric", or "no_trace"
        dataset_key: Dataset identifier (e.g., "telecom"). Telecom has no log data.
    """
    # Telecom dataset has no log data regardless of condition
    has_logs = dataset_key is None or "telecom" not in dataset_key
    enable_log = has_logs and condition != "no_log"
    enable_metric = condition != "no_metric"
    enable_trace = condition != "no_trace"

    access_items = []
    if enable_log:
        access_items.append("- Logs: system and application log records")
    if enable_metric:
        access_items.append("- Metrics: time-series performance metrics")
    if enable_trace:
        access_items.append("- Traces: distributed tracing data")

    # Build restriction notice for disabled types
    disabled_names = []
    if not enable_log:
        disabled_names.append("log")
    if not enable_metric:
        disabled_names.append("metric")
    if not enable_trace:
        disabled_names.append("trace")

    restriction = ""
    if disabled_names:
        restriction = (
            "\n\nIMPORTANT: Only the telemetry types listed above are available. "
            + ", ".join(f"{t.capitalize()} data" for t in disabled_names)
            + " is NOT available for this system. Do NOT attempt to access or analyze "
            + ", ".join(f"{t}" for t in disabled_names)
            + " data."
        )

    return (
        "How to analyze telemetry data:\n"
        "Use the available APIs to retrieve and analyze telemetry data.\n"
        "In each step, provide one clear, atomic API call.\n"
        "\n"
        "Available telemetry data types for this run:\n"
        + "\n".join(access_items) + "\n"
        + restriction + "\n"
        "\n"
        "Guidelines:\n"
        "1. Use one API call per response.\n"
        "2. Reuse cached results when possible.\n"
        "3. Use UTC timestamps consistently.\n"
        "\n"
        "When you have completed your analysis, submit your findings:\n"
        "```\n"
        'submit({{"1": {{"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", '
        '"root cause component": "component_name", "root cause reason": "reason"}}}})\n'
        "```"
    )


# Backward-compatible alias (all types enabled)
TELEMETRY_GUIDE = build_executor_telemetry_guide()
