from __future__ import annotations

from pathlib import Path

import pandas as pd


_COLUMN_MEANINGS = {
    "timestamp": "Unix timestamp in seconds.",
    "cmdb_id": "Component identifier. For Market pod metrics this may be node-X.pod_name.",
    "kpi_name": "Metric name.",
    "name": "Metric name column used by some datasets.",
    "value": "Metric value for the timestamp/component/KPI row.",
    "span_id": "Unique span id.",
    "id": "Span id used by some trace schemas.",
    "trace_id": "Trace identifier.",
    "parent_span": "Parent span id used to reconstruct caller-callee linkage.",
    "pid": "Parent span id used by some trace schemas.",
    "duration": "Span latency / elapsed time.",
    "elapsedTime": "Trace elapsed time in microseconds/milliseconds depending on dataset.",
    "status_code": "Trace status. Success often appears as 0/OK/200/SUCCESS.",
    "callType": "Trace call type (e.g., CSF / RemoteProcess).",
    "dsName": "Downstream service/component name in some trace schemas.",
    "type": "Trace span type/call type.",
    "operation_name": "Operation or endpoint name.",
    "message": "Log message body.",
    "level": "Log severity level.",
}


def _column_meaning_lines(columns: list[str]) -> str:
    lines = []
    for col in columns:
        meaning = _COLUMN_MEANINGS.get(col, "Column from dataset telemetry.")
        lines.append(f"- {col}: {meaning}")
    return "\n".join(lines)


def _read_csv_preview(path: Path, *, rows: int = 5) -> str:
    if not path.exists():
        return ""
    try:
        df = pd.read_csv(path, nrows=rows)
    except Exception as exc:
        return f"{path.name}: failed to read preview ({exc})"
    cols = [str(c) for c in df.columns.tolist()]
    preview = df.head(rows).to_string(index=False)
    return (
        f"[{path.name}]\n"
        f"columns: {', '.join(cols)}\n"
        f"column_meanings:\n{_column_meaning_lines(cols)}\n"
        f"head:\n{preview}\n"
    )


def build_executor_telemetry_context(actions, namespace: str) -> str:
    base_path = getattr(getattr(actions, "static_app", None), "base_path", None)
    if base_path is None:
        return "No local telemetry base path available."
    ns_path = Path(base_path) / namespace
    snippets: list[str] = []
    candidate_paths = [
        ns_path / "metrics" / "metric_node.csv",
        ns_path / "metrics" / "metric_container.csv",
        ns_path / "metrics" / "metric_service.csv",
        ns_path / "metrics" / "metric_app.csv",
        ns_path / "metrics" / "metric_mesh.csv",
        ns_path / "traces" / "trace_span.csv",
    ]
    logs_dir = ns_path / "logs"
    if logs_dir.exists():
        first_log = next(iter(sorted(logs_dir.glob("*.csv"))), None)
        if first_log is not None:
            candidate_paths.append(first_log)
    for path in candidate_paths:
        snippet = _read_csv_preview(path)
        if snippet:
            snippets.append(snippet)
    return "\n".join(snippets) if snippets else "No CSV preview available."


def build_dataset_format_guide(dataset_name: str) -> str:
    name = str(dataset_name or "").lower()
    if name.startswith("openrca_market"):
        return (
            "- Metrics are split across metric_node.csv / metric_container.csv / metric_service.csv / metric_mesh.csv.\n"
            "- Market pod metrics often use cmdb_id format node-X.<pod_name> while traces/logs may use <pod_name> only.\n"
            "- When joining signals across files, match both exact cmdb_id and pod suffix.\n"
            "- Trace data in trace_span.csv is the primary source for caller-callee relation direction."
        )
    if name.startswith("openrca_telecom"):
        return (
            "- Components are commonly os_*, docker_*, db_*.\n"
            "- Metrics are usually in metric_node.csv / metric_container.csv / metric_service.csv / metric_middleware.csv.\n"
            "- Trace relation evidence should come from trace_span.csv call chain linkage.\n"
            "- For DB/network checks, correlate tnsping/session/queue-like metrics with trace timing."
        )
    if name.startswith("openrca_bank"):
        return (
            "- Metrics are mainly app/container oriented with JVM/DB/network counters depending on component.\n"
            "- Trace/log evidence should be used to verify transaction-path direction and timing alignment.\n"
            "- Prefer combining trace edge anomalies with app/JVM metric corroboration."
        )
    return (
        "- Use metric_*.csv for KPI evidence, trace_span.csv for relation direction/timing, "
        "and logs/*.csv for corroborating events."
    )

