"""Dataset-specific profiles for the staged RCA pipeline.

Each profile defines:
  - KPIs to scan per component type (for get_kpi_peer_graph)
  - KPI → reason mapping
  - Reasons grouped by component level
  - Vision critic prompt

Loaded from dataset JSON config + hardcoded KPI knowledge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DatasetProfile:
    name: str
    kpis_by_type: dict[str, list[str]]          # comp_type → [kpi, ...] (core/chart KPIs)
    kpi_to_reasons: dict[str, list[str]]        # kpi → [reason, ...]
    reasons_by_level: dict[str, list[str]]      # level → [reason, ...]
    component_levels: dict[str, list[str]]      # level → [component, ...]
    possible_components: list[str] = field(default_factory=list)
    possible_reasons: list[str] = field(default_factory=list)
    vision_critic_prompt: str = ""
    # Dataset-wide non-dead KPI catalog for deep dive executor hints:
    # level -> reason -> [kpi, ...] (built from full metric catalog, not just core KPIs)
    reason_kpis_by_level: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # Full non-dead KPIs by metric file type (e.g., "node", "container", "service").
    # Populated from kpi_catalog_*.json: kpis: {type: [kpi, ...]}.
    full_kpis_by_type: dict[str, list[str]] = field(default_factory=dict)


# =====================================================================
# Market Cloudbed-1 / Cloudbed-2
# =====================================================================

_MARKET_KPIS_BY_TYPE = {
    "node": [
        "system.cpu.pct_usage",
        "system.io.r_s",
        "system.io.w_s",
        "system.disk.used",
        "system.disk.pct_usage",
        "system.mem.pct_usage"
    ],
    "pod": [
        "container_threads",
        "container_cpu_usage_seconds",
        "container_memory_usage_MB",
        "container_fs_reads./dev/vda",
        "container_fs_writes./dev/vda",
        "container_fs_writes./dev/vda1",
        "container_network_receive_packets.eth0",
        "container_network_receive_MB.eth0",
    ],
}

_MARKET_KPI_TO_REASONS: dict[str, list[str]] = {
    "system.cpu.pct_usage":      ["node CPU load", "node CPU spike"],
    "system.mem.used":           ["node memory consumption"],
    "system.io.r_s":             ["node disk read I/O consumption"],
    "system.io.w_s":             ["node disk write I/O consumption"],
    "system.disk.used":          ["node disk space consumption"],
    "system.disk.pct_usage":     ["node disk space consumption"],
    "container_threads":                                 [],
    "container_cpu_usage_seconds":                      ["container CPU load"],
    "container_memory_usage_MB":                        ["container memory load"],
    "container_fs_reads./dev/vda":                      ["container read I/O load"],
    "container_fs_writes./dev/vda":                     ["container write I/O load"],
    "container_fs_writes./dev/vda1":                    ["container write I/O load"],
    "container_network_receive_packets.eth0":           [],
    "container_network_receive_MB.eth0":                [],
    "trace_latency":  ["container network latency"],
    "trace_errors":   ["container network packet retransmission"],
}

_MARKET_REASONS_BY_LEVEL: dict[str, list[str]] = {
    "node": [
        "node CPU load", "node CPU spike", "node memory consumption",
        "node disk read I/O consumption", "node disk write I/O consumption",
        "node disk space consumption",
    ],
    "pod": [
        "container CPU load", "container memory load",
        "container network packet retransmission",
        "container network packet corruption", "container network latency",
        "container packet loss", "container process termination",
        "container read I/O load", "container write I/O load",
    ],
    "service": [
        "container CPU load", "container memory load",
        "container network packet retransmission",
        "container network packet corruption", "container network latency",
        "container packet loss", "container process termination",
        "container read I/O load", "container write I/O load",
    ],
}

_MARKET_VISION_CRITIC = """\
You are a time-series graph analysis expert for a Kubernetes-based microservice system.
Examine the attached peer comparison chart and identify outlier components.

## Component naming
- Node metrics: cmdb_id = node-1, node-2, ..., node-6
- Pod/Container metrics: cmdb_id = node-X.pod-name (e.g., node-5.frontend-1)

## Rules
1. Consistently high/low throughout (flat or stable with no distinct change point) is NOT an outlier.
2. Cyclical or repeating patterns are NOT outliers. A periodic up–down (wave-like) pattern that repeats over the window is normal. Do NOT report multiple similar spikes/drops on the SAME component that repeat over the time window; if the whole series is just oscillating, ignore it entirely.
3. Minor fluctuations (small jitter, no clear step or spike) are NOT outliers.
4. A clear one-time step down (drop then stay low) or step up (rise then stay high) IS an outlier. Report each affected component with high severity.
5. It is fine to report NO outliers only when there is truly no distinct one-time change.
"""


# =====================================================================
# Telecom
# =====================================================================

_TELECOM_KPIS_BY_TYPE: dict[str, list[str]] = {
    "docker": [
        "container_cpu_used"
    ],
    "os": [
        "Memory_used_pct",
        "Disk_io_util",
        "Sent_queue",
        "Received_queue",
    ],
    "db": [
        "Sess_Connect",
        "Proc_Used_Pct",
        "Proc_User_Used_Pct",
        "On_Off_State",
        "tnsping_result_time",
    ],
}

_TELECOM_KPI_TO_REASONS: dict[str, list[str]] = {
    "container_cpu_used":  ["CPU fault"],
    "container_mem_used":  [],
    "ICMP_ping":           ["network delay", "network loss"],
    "Memory_used_pct":     [],
    "Disk_io_util":        [],
    "Sent_queue":          ["network delay", "network loss"],
    "Received_queue":      ["network delay", "network loss"],
    "Sess_Connect":        ["db connection limit"],
    "Session_pct":         ["db connection limit"],
    "Proc_Used_Pct":       ["db connection limit"],
    "Proc_User_Used_Pct":  ["db connection limit"],
    "On_Off_State":        ["db close"],
    "tnsping_result_time": ["db close", "network delay"],
    "trace_latency":       ["network delay"],
    "trace_errors":        ["network loss"],
}

_TELECOM_REASONS_BY_LEVEL: dict[str, list[str]] = {
    "node": ["CPU fault", "network delay", "network loss"],
    "pod":  ["CPU fault", "network delay", "network loss"],
    "service": ["CPU fault", "network delay", "network loss",
                "db connection limit", "db close"],
}

_TELECOM_VISION_CRITIC = """\
You are a time-series graph analysis expert for a Telecom microservice system.
Examine the attached peer comparison chart and identify outlier components.

## Component naming
- OS nodes: os_001 ~ os_022
- Docker containers: docker_001 ~ docker_008
- Database services: db_001 ~ db_013

## Rules
1. Consistently high/low throughout (flat or stable with no distinct change point) is NOT an outlier.
2. Cyclical or repeating patterns are NOT outliers. A periodic up–down (wave-like) pattern that repeats over the window is normal. Do NOT report multiple similar spikes/drops on the SAME component that repeat over the time window; if the whole series is just oscillating, ignore it entirely.
3. Minor fluctuations (small jitter, no clear step or spike) are NOT outliers.
4. A clear one-time step down (drop then stay low) or step up (rise then stay high) IS an outlier. Report each affected component with high severity.
5. It is fine to report NO outliers only when there is truly no distinct one-time change.
"""


# =====================================================================
# Bank
# =====================================================================

_BANK_KPIS_BY_TYPE: dict[str, list[str]] = {
    "node": [
        "OSLinux-CPU_CPU_CPUCpuUtil",
        "OSLinux-CPU_CPU-0_SingleCpuUtil",
        # "OSLinux-OSLinux_MEMORY_MEMORY_MEMUsedMemPerc",
        # "OSLinux-OSLinux_MEMORY_MEMORY_NoCacheMemPerc",
        # "OSLinux-OSLinux_MEMORY_MEMORY_MEMFreeMem",
        # "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKReadWrite",
        # "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdb_DSKRead",
        # "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdb_DSKReadWrite",
        # "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdb_DSKRTps",
        # "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKRTps",
        # "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKRead",
        # "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKBps",
        # "OSLinux-OSLinux_NETWORK_NETWORK_TCP-FIN-WAIT",
        # "OSLinux-OSLinux_NETWORK_ens160_NETBandwidthUtil",
        # "OSLinux-OSLinux_NETWORK_NETWORK_TotalTcpConnNum",
        # "OSLinux-OSLinux_NETWORK_ens160_NETPacketsOut",
        # "OSLinux-OSLinux_NETWORK_ens160_NETPacketsIn",
        # "OSLinux-OSLinux_NETWORK_ens160_NETKBTotalPerSec",
        # "OSLinux-OSLinux_NETWORK_NETWORK_TCP-CLOSE-WAIT",
        # "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKPercentBusy",
    ],
    "jvm": [
        # "JVM-Operating System_7779_JVM_JVM_CPULoad",
        # "JVM-Operating System_7778_JVM_JVM_CPULoad",
        # "JVM-Memory_7778_JVM_Memory_NoHeapMemoryUsed",
        # "JVM-Memory_7779_JVM_Memory_NoHeapMemoryUsed",
        # "JVM-Memory_7779_JVM_Memory_HeapMemoryUsage",
        # "JVM-Memory_7778_JVM_Memory_HeapMemoryUsage",
        # "JVM-Memory_7778_JVM_Memory_HeapMemoryUsed",
        # "JVM-Memory_7779_JVM_Memory_HeapMemoryUsed",
    ],
}

_BANK_KPI_TO_REASONS: dict[str, list[str]] = {
    "OSLinux-CPU_CPU_CPUCpuUtil":                       ["high CPU usage"],
    "OSLinux-CPU_CPU-0_SingleCpuUtil":                  ["high CPU usage"],
    "OSLinux-OSLinux_MEMORY_MEMORY_MEMUsedMemPerc":     ["high memory usage"],
    "OSLinux-OSLinux_MEMORY_MEMORY_NoCacheMemPerc":     ["high memory usage"],
    "OSLinux-OSLinux_MEMORY_MEMORY_MEMFreeMem":         ["high memory usage"],
    "OSLinux-OSLinux_NETWORK_NETWORK_TotalTcpConnNum":  ["network latency",
                                                         "network packet loss"],
    "OSLinux-OSLinux_NETWORK_NETWORK_TCP-FIN-WAIT":     ["network latency",
                                                         "network packet loss"],
    "OSLinux-OSLinux_NETWORK_ens160_NETBandwidthUtil":  ["network latency",
                                                         "network packet loss"],
    "OSLinux-OSLinux_NETWORK_ens160_NETPacketsOut":     ["network latency",
                                                         "network packet loss"],
    "OSLinux-OSLinux_NETWORK_ens160_NETPacketsIn":      ["network latency",
                                                         "network packet loss"],
    "OSLinux-OSLinux_NETWORK_ens160_NETKBTotalPerSec":  ["network latency",
                                                         "network packet loss"],
    "OSLinux-OSLinux_NETWORK_NETWORK_TCP-CLOSE-WAIT":   ["network latency",
                                                         "network packet loss"],
    "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKReadWrite": ["high disk I/O read usage"],
    "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdb_DSKRead":      ["high disk I/O read usage"],
    "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdb_DSKReadWrite": ["high disk I/O read usage"],
    "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sdb_DSKRTps":      ["high disk I/O read usage"],
    "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKRTps":      ["high disk I/O read usage"],
    "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKRead":      ["high disk I/O read usage"],
    "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKBps":       ["high disk I/O read usage"],
    "OSLinux-OSLinux_LOCALDISK_LOCALDISK-sda_DSKPercentBusy": ["high disk I/O read usage"],
    "JVM-Operating System_7779_JVM_JVM_CPULoad":         ["high JVM CPU load"],
    "JVM-Operating System_7778_JVM_JVM_CPULoad":         ["high JVM CPU load"],
    "JVM-Memory_7778_JVM_Memory_NoHeapMemoryUsed":       ["JVM Out of Memory (OOM) Heap"],
    "JVM-Memory_7779_JVM_Memory_NoHeapMemoryUsed":       ["JVM Out of Memory (OOM) Heap"],
    "JVM-Memory_7779_JVM_Memory_HeapMemoryUsage":        ["JVM Out of Memory (OOM) Heap"],
    "JVM-Memory_7778_JVM_Memory_HeapMemoryUsage":        ["JVM Out of Memory (OOM) Heap"],
    "JVM-Memory_7778_JVM_Memory_HeapMemoryUsed":         ["JVM Out of Memory (OOM) Heap"],
    "JVM-Memory_7779_JVM_Memory_HeapMemoryUsed":         ["JVM Out of Memory (OOM) Heap"],
}

_BANK_REASONS_BY_LEVEL: dict[str, list[str]] = {
    "node": [
        "high CPU usage", "high memory usage",
        "network latency", "network packet loss",
        "high disk I/O read usage", "high disk space usage",
        "high JVM CPU load", "JVM Out of Memory (OOM) Heap",
    ],
}

_BANK_VISION_CRITIC = """\
You are a time-series graph analysis expert for a banking microservice system.
Examine the attached peer comparison chart and identify outlier components.

## Component naming
- Components: apache01/02, Tomcat01~04, MG01/02, IG01/02, Mysql01/02, Redis01/02

## Rules
1. An outlier must show a visible CHANGE — sudden spike, sharp drop, or shift.
2. Consistently high/low is NOT an outlier.
3. Cyclical or repeating patterns are NOT outliers. In particular, a periodic up–down (oscillating) pattern over time should be treated as normal. 
Do NOT report multiple similar spikes/drops on the SAME component that repeat over the time window. 
If a component oscillates repeatedly, exclude it — do not list each event as a separate outlier.
4. Minor fluctuations are NOT outliers.
5. Prefer at most one outlier event per component. It is fine to report NO outliers.

## Fault pattern reference
- high CPU usage: OSLinux CPU util sustained spike
- high memory usage: memory used percent sustained rise
- network latency/loss: TCP connection count spike, trace latency spike
- high disk I/O read usage: disk percent busy spike
- high disk space usage: filesystem used space rises toward capacity
- high JVM CPU load: JVM CPU load sustained spike
- JVM OOM Heap: JVM heap memory usage rises toward max
"""


# =====================================================================
# Factory
# =====================================================================

def build_profile(
    dataset_key: str,
    config_path: str | Path | None = None,
) -> DatasetProfile:
    """Build a DatasetProfile from dataset key (e.g., 'openrca_market_cb1').

    Loads component_levels and possible_root_causes from the JSON config.
    KPIs and vision critic prompts come from hardcoded per-dataset definitions.
    """
    cfg: dict = {}
    prc: dict = {}
    if config_path and Path(config_path).exists():
        cfg = json.loads(Path(config_path).read_text())
        prc = cfg.get("possible_root_causes", {})

    component_levels = prc.get("component_levels", {})
    possible_components = prc.get("components", [])
    possible_reasons = prc.get("reasons", [])

    ds = dataset_key.replace("openrca_", "")

    # Helper: build level→reason→[kpi] from a full KPI catalog and kpi_to_reasons.
    def _build_reason_kpis_by_level(
        full_kpis: list[str],
        kpi_to_reasons: dict[str, list[str]],
        reasons_by_level: dict[str, list[str]],
    ) -> dict[str, dict[str, list[str]]]:
        mapping: dict[str, dict[str, list[str]]] = {}
        for level, reasons in reasons_by_level.items():
            level_map: dict[str, list[str]] = {}
            for r in reasons:
                attached: list[str] = []
                for k in full_kpis:
                    if r in kpi_to_reasons.get(k, []):
                        attached.append(k)
                if attached:
                    level_map[r] = attached
            if level_map:
                mapping[level] = level_map
        return mapping

    # Try to load precomputed full KPI catalogs (non-dead KPIs) if available.
    base_dir = Path(__file__).parent
    catalog_path: Path | None = None
    if ds.startswith("market"):
        catalog_path = base_dir / "kpi_catalog_market.json"
    elif ds.startswith("telecom"):
        catalog_path = base_dir / "kpi_catalog_telecom.json"
    elif ds.startswith("bank"):
        catalog_path = base_dir / "kpi_catalog_bank.json"

    full_kpis: list[str] = []
    full_kpis_by_type: dict[str, list[str]] = {}
    if catalog_path and catalog_path.exists():
        try:
            catalog = json.loads(catalog_path.read_text())
            kpi_obj = catalog.get("kpis", {})
            # Support both old flat-list format and new type→[kpi] dict.
            if isinstance(kpi_obj, dict):
                acc: set[str] = set()
                typed: dict[str, list[str]] = {}
                for vals in kpi_obj.values():
                    for k in vals:
                        acc.add(str(k))
                for t, vals in kpi_obj.items():
                    typed[t] = [str(k) for k in vals]
                full_kpis = sorted(acc)
                full_kpis_by_type = typed
            else:
                full_kpis = list(kpi_obj or [])
        except Exception:
            full_kpis = []

    if ds.startswith("market"):
        kpis_by_type = _MARKET_KPIS_BY_TYPE
        kpi_to_reasons = _MARKET_KPI_TO_REASONS
        reasons_by_level = _MARKET_REASONS_BY_LEVEL
        # Fallback: if no catalog, limit to KPIs known in mapping.
        if not full_kpis:
            full_kpis = sorted(kpi_to_reasons.keys())
        reason_kpis_by_level = _build_reason_kpis_by_level(
            full_kpis, kpi_to_reasons, reasons_by_level
        )
        # Service-level reasons share pod-level KPIs in Market by default.
        if "pod" in reason_kpis_by_level and "service" not in reason_kpis_by_level:
            reason_kpis_by_level["service"] = reason_kpis_by_level["pod"]
        return DatasetProfile(
            name=dataset_key,
            kpis_by_type=kpis_by_type,
            kpi_to_reasons=kpi_to_reasons,
            reasons_by_level=reasons_by_level,
            component_levels=component_levels,
            possible_components=possible_components,
            possible_reasons=possible_reasons,
            vision_critic_prompt=_MARKET_VISION_CRITIC,
            reason_kpis_by_level=reason_kpis_by_level,
            full_kpis_by_type=full_kpis_by_type,
        )

    if ds.startswith("telecom"):
        if not component_levels:
            component_levels = {
                "node": [f"os_{i:03d}" for i in range(1, 23)],
                "pod": [f"docker_{i:03d}" for i in range(1, 9)],
                "service": [f"db_{i:03d}" for i in range(1, 14)],
            }
        kpis_by_type = _TELECOM_KPIS_BY_TYPE
        kpi_to_reasons = _TELECOM_KPI_TO_REASONS
        reasons_by_level = _TELECOM_REASONS_BY_LEVEL
        if not full_kpis:
            full_kpis = sorted(kpi_to_reasons.keys())
        reason_kpis_by_level = _build_reason_kpis_by_level(
            full_kpis, kpi_to_reasons, reasons_by_level
        )
        return DatasetProfile(
            name=dataset_key,
            kpis_by_type=kpis_by_type,
            kpi_to_reasons=kpi_to_reasons,
            reasons_by_level=reasons_by_level,
            component_levels=component_levels,
            possible_components=possible_components,
            possible_reasons=possible_reasons,
            vision_critic_prompt=_TELECOM_VISION_CRITIC,
            reason_kpis_by_level=reason_kpis_by_level,
            full_kpis_by_type=full_kpis_by_type,
        )

    if ds.startswith("bank"):
        if not component_levels:
            component_levels = {"node": possible_components}
        kpis_by_type = _BANK_KPIS_BY_TYPE
        kpi_to_reasons = _BANK_KPI_TO_REASONS
        reasons_by_level = _BANK_REASONS_BY_LEVEL
        if not full_kpis:
            full_kpis = sorted(kpi_to_reasons.keys())
        reason_kpis_by_level = _build_reason_kpis_by_level(
            full_kpis, kpi_to_reasons, reasons_by_level
        )
        return DatasetProfile(
            name=dataset_key,
            kpis_by_type=kpis_by_type,
            kpi_to_reasons=kpi_to_reasons,
            reasons_by_level=reasons_by_level,
            component_levels=component_levels,
            possible_components=possible_components,
            possible_reasons=possible_reasons,
            vision_critic_prompt=_BANK_VISION_CRITIC,
            reason_kpis_by_level=reason_kpis_by_level,
            full_kpis_by_type=full_kpis_by_type,
        )

    raise ValueError(f"Unknown dataset: {dataset_key}")
