from __future__ import annotations

from dataclasses import dataclass

from clients.tree_traversal.tools.deep_dive import bank, market_cb1, telecom


@dataclass(frozen=True)
class DatasetToolSpec:
    name: str
    description: str
    required_args: tuple[str, ...]


_BASE_SPECS = [
    DatasetToolSpec(
        name="get_component_kpis_minutely",
        description="Fetch minute-bucket KPI values for one component and KPI list. Returns pandas table (time index, columns=kpi_agg). Optional agg: mean|min|max|p50|p95|p99.",
        required_args=("component", "kpis", "start_time", "end_time"),
    ),
    DatasetToolSpec(
        name="get_peer_kpi_minutely",
        description="Fetch minute-bucket values for one KPI across target component and peers. Returns pandas table (time index, columns=component[_agg]). Optional agg: mean|min|max|p50|p95|p99.",
        required_args=("kpi", "target_component", "start_time", "end_time"),
    ),
]


def get_deep_dive_tool_specs(dataset_name: str) -> list[DatasetToolSpec]:
    name = str(dataset_name or "").lower()
    if name.startswith("openrca_market"):
        return list(_BASE_SPECS)
    if name.startswith("openrca_telecom"):
        return list(_BASE_SPECS)
    if name.startswith("openrca_bank"):
        return list(_BASE_SPECS)
    return []


def format_deep_dive_tool_guide(dataset_name: str) -> str:
    specs = get_deep_dive_tool_specs(dataset_name)
    if not specs:
        return "(none)"
    lines = []
    for spec in specs:
        lines.append(f"- {spec.name}: {spec.description}")
        lines.append(f"  required_args: {', '.join(spec.required_args)}")
    return "\n".join(lines)


def dispatch_deep_dive_tool(dataset_name: str, tool_name: str, tool_args: dict) -> object:
    name = str(dataset_name or "").lower()
    t = str(tool_name or "").strip()
    args = dict(tool_args or {})

    if name.startswith("openrca_market"):
        if t == "get_component_kpis_minutely":
            return market_cb1.get_component_kpis_minutely(**args)
        if t == "get_peer_kpi_minutely":
            return market_cb1.get_peer_kpi_minutely(**args)

    if name.startswith("openrca_telecom"):
        if t == "get_component_kpis_minutely":
            return telecom.get_component_kpis_minutely(**args)
        if t == "get_peer_kpi_minutely":
            return telecom.get_peer_kpi_minutely(**args)

    if name.startswith("openrca_bank"):
        if t == "get_component_kpis_minutely":
            return bank.get_component_kpis_minutely(**args)
        if t == "get_peer_kpi_minutely":
            return bank.get_peer_kpi_minutely(**args)

    available = [x.name for x in get_deep_dive_tool_specs(dataset_name)]
    raise ValueError(
        f"Unsupported deep_dive tool_name={tool_name!r} for dataset={dataset_name!r}. "
        f"available_tools={available!r}"
    )
