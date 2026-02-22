"""Agent-specific telemetry access guide for OpenRCA RCA Agent.

Injected into the task description's {telemetry_guide} placeholder.
Describes how the Controller-Executor architecture accesses telemetry data.
"""


def build_telemetry_guide(condition="all"):
    """Build telemetry guide filtered by ablation condition.

    Args:
        condition: "all", "no_log", "no_metric", or "no_trace"
    """
    enable_log = condition != "no_log"
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
        "You work with an Executor that writes and executes Python code to analyze telemetry data.\n"
        "In each step, provide a clear, atomic instruction for the Executor.\n"
        "\n"
        "The Executor can access:\n"
        + "\n".join(access_items) + "\n"
        + restriction + "\n"
        "\n"
        "Provide one instruction per step. The Executor will:\n"
        "1. Generate Python code based on your instruction\n"
        "2. Fetch and analyze the telemetry data\n"
        "3. Return a summary of the results\n"
        "\n"
        "When you have completed your analysis, submit your findings:\n"
        "```\n"
        'submit({{"1": {{"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", '
        '"root cause component": "component_name", "root cause reason": "reason"}}}})\n'
        "```"
    )


# Default (all types) for backward compatibility
TELEMETRY_GUIDE = build_telemetry_guide("all")
