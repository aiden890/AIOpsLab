from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DatasetToolSpec:
    name: str
    description: str
    required_args: tuple[str, ...]


def get_trace_expand_tool_specs(dataset_name: str) -> list[DatasetToolSpec]:
    name = str(dataset_name or "").lower()
    if name.startswith("openrca_market"):
        return [
            DatasetToolSpec(
                name="get_edge_latency_minutely",
                description=(
                    "Return pandas minute-table of edge latency for a focus component. "
                    "direction=caller|callee, source=trace_span|mesh|both|auto, "
                    "columns={edge}_{agg}[_{trace|mesh}]"
                ),
                required_args=("focus_component", "direction", "start_time", "end_time"),
            ),
            DatasetToolSpec(
                name="get_edge_error_rate_minutely",
                description=(
                    "Return pandas minute-table of edge error rate for a focus component. "
                    "direction=caller|callee, source=trace_span|mesh|both|auto, "
                    "columns={edge}_{agg}[_{trace|mesh}]"
                ),
                required_args=("focus_component", "direction", "start_time", "end_time"),
            ),
        ]
    if name.startswith("openrca_telecom"):
        return [
            DatasetToolSpec(
                name="telecom_trace_tools_todo",
                description="Telecom-specific trace expand tools are not implemented yet.",
                required_args=(),
            )
        ]
    if name.startswith("openrca_bank"):
        return [
            DatasetToolSpec(
                name="bank_trace_tools_todo",
                description="Bank-specific trace expand tools are not implemented yet.",
                required_args=(),
            )
        ]
    return []


def format_trace_expand_tool_guide(dataset_name: str) -> str:
    specs = get_trace_expand_tool_specs(dataset_name)
    if not specs:
        return "(none)"
    lines: list[str] = []
    for spec in specs:
        req = ", ".join(spec.required_args) if spec.required_args else "(none)"
        lines.append(f"- {spec.name}: {spec.description}")
        lines.append(f"  required_args: {req}")
    return "\n".join(lines)
