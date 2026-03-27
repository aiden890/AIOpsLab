"""Market Cloudbed-1/2 specific workflow and vision critic prompts.

Component hierarchy:
  - node: node-1 ~ node-6  (physical/virtual machines)
  - pod:  node-X.service-Y  (container instances running on nodes)
  - service: frontend, cartservice, ...  (logical grouping of pods)

Metric files:
  - metric_node.csv      → cmdb_id: node-1, KPIs: system.cpu.*, system.mem.*, system.io.*, ...
  - metric_container.csv → cmdb_id: node-5.frontend-1, KPIs: container_cpu_*, container_memory_*, ...
  - metric_service.csv   → service: frontend-http, columns: sr, mrt, rr, count
  - metric_runtime.csv   → cmdb_id: adservice.ts:8088, KPIs: java_nio_*
  - metric_mesh.csv      → cmdb_id: cartservice-1.source.cartservice.redis-cart
  - logs.csv             → timestamp, service/cmdb_id, log text fields for restart/error/event clues

Trace:
  - trace_span.csv → timestamp, cmdb_id, span_id, trace_id, duration, type, status_code, operation_name, parent_span
  - cmdb_id in traces = pod name (e.g., frontend-0), NOT node-X.pod format
  - status_code 0 = success
"""

WORKFLOW_MARKET_WITH_VIZ = """\
**Step 0 — Narrow down the fault time window with service-level success rate**
Use `get_success_rate_drop_graph(namespace)` to plot the service-level success rate (sr) over time. This reads metric_service.csv and highlights time windows where SR drops. Identify:
  - WHICH services experienced SR drops
  - WHEN the drops occurred (the red-shaded regions)
  - The severity of the drops
This narrows down the fault time window BEFORE scanning individual KPIs.

**Step 1 — Scan ALL component levels with peer graphs**
Use `get_kpi_peer_graph(namespace, component_type, kpi_name)` to visually scan for outliers. `kpi_name` can be a single string or a list of up to 2 KPIs. You MUST check all of the following levels:

  Node level (component_type="node"):
  - system.cpu.pct_usage  (node CPU load / spike)
  - system.mem.pct_usage  (node memory consumption)
  - system.io.util        (node disk I/O — both read and write)
  - system.disk.pct_usage (node disk space consumption)
  - system.net.tcp.retrans_segs  (node network retransmission)
  - ping.can_connect      (node network connectivity)

  Pod level (component_type="pod"):
  - container_cpu_usage_seconds    (container CPU load)
  - container_memory_usage_MB      (container memory load)
  - container_fs_reads_MB./dev/vda (container read I/O load)
  - container_fs_writes_MB./dev/vda (container write I/O load)
  - container_network_transmit_packets_dropped.eth0 (container packet loss)
  - container_network_receive_errors.eth0 (container network errors)

After each graph, the vision critic will identify outliers. Collect all outlier components found across all levels.

Do NOT use trace graphs during initial localization. Initial localization must be based on metrics only.
Trace graphs are allowed only after metric-based candidates have already been localized and you are doing follow-up causal analysis.

IMPORTANT about node-pod mapping:
  - Pod metric cmdb_id format is "node-X.pod-name" (e.g., node-5.frontend-1). This tells you WHICH node each pod runs on.
  - If multiple pods on the SAME node show anomalies, the root cause is likely the NODE itself, not individual pods. Check node-level KPIs to confirm.
  - If only ONE pod shows an anomaly while other pods on the same node are fine, the root cause is at the pod or service level.
  - Container network faults (packet retransmission, corruption, latency, loss) may appear at the service level — check if ALL pods of a service are affected.

**Step 1b — Filter outliers**
Review the collected outliers and REMOVE false positives:
  - A brief spike that appears and disappears within 1-2 minutes is transient noise, NOT a fault.
  - A component that is consistently high/low for the ENTIRE window is just its normal baseline.
  - Periodic or cyclical patterns shared with peers are normal behavior.
Only keep outliers that show a SUSTAINED behavioral change (lasting 3+ minutes) that clearly differs from peers. If no valid outlier remains for a component type after filtering, go back to Step 1 and check additional KPIs for that type.

**Step 2 — Determine the root cause LEVEL (node vs pod vs service)**
Based on the outliers found:
  - If node-level KPIs (system.cpu.*, system.mem.*, system.io.*, system.disk.*) show a specific node anomaly → root cause is at NODE level (e.g., "node-4")
  - If only a specific pod shows an anomaly (container_cpu_*, container_memory_*, container_fs_*) while other pods on the same node are fine → root cause is at POD level (e.g., "frontend-1")
  - If ALL pods of a service show anomalies (container_network_* across all pods) → root cause is at SERVICE level (e.g., "frontend")
Use `get_kpi_peer_graph` or `execute()` to confirm the anomaly is real and sustained at the determined level.

**Step 3 — Identify the failure reason**
For the identified root cause component, determine the specific failure reason from the list of possible root causes.
Match the anomalous KPI to the reason:
  Node reasons:
  - system.cpu.pct_usage spike → "node CPU load" or "node CPU spike"
  - system.mem.pct_usage spike → "node memory consumption"
  - system.io.r_s / system.io.rkb_s spike → "node disk read I/O consumption"
  - system.io.w_s spike → "node disk write I/O consumption"
  - system.disk.pct_usage spike → "node disk space consumption"
  Container/Service reasons:
  - container_cpu_usage_seconds spike → "container CPU load"
  - container_memory_usage_MB spike → "container memory load"
  - container_network + retrans → "container network packet retransmission"
  - container_network + errors → "container network packet corruption"
  - container_network + latency in traces → "container network latency"
  - container_network + packet drop → "container packet loss"
  - container_processes drops → "container process termination"
  - container_fs_reads_MB spike → "container read I/O load"
  - container_fs_writes_MB spike → "container write I/O load"

If the candidate suggests restart/termination, traffic cutoff, or network isolation, briefly inspect `logs.csv`
with `execute()` or `telemetry.get_logs()` for corroborating clues such as restarts, connection errors,
readiness/liveness failures, shutdown messages, or routing/policy-related errors. Logs are supporting evidence,
not the primary localization signal.

**Step 4 — Pinpoint the exact occurrence time**
Find the exact timestamp when the anomaly first starts. Report the precise "YYYY-MM-DD HH:MM:SS" from the CSV timestamp column.

**Step 5 — Submit your conclusion**
Once you have identified the root cause component, the specific reason, and the exact occurrence time from the CSV, submit your answer. Do NOT delegate conclusion to `execute()` — that is your job.\
"""

WORKFLOW_MARKET_WITHOUT_VIZ = """\
**Step 0 — Narrow down the fault time window with service-level success rate**
Use `execute()` to load metric_service.csv and compute the success rate (sr) per service over time (1-minute buckets). Identify:
  - WHICH services experienced SR drops
  - WHEN the drops occurred (exact time buckets)
  - The severity of the drops (minimum SR value)

**Step 1 — Scan ALL component levels for anomalies**
Use `execute()` to load metric CSVs and compute per-component statistics. You MUST check all of the following:

  Node level (metric_node.csv, cmdb_id: node-1~6):
  - system.cpu.pct_usage, system.mem.pct_usage, system.io.util, system.disk.pct_usage
  - system.net.tcp.retrans_segs, ping.can_connect

  Pod level (metric_container.csv, cmdb_id: node-X.pod-name):
  - container_cpu_usage_seconds, container_memory_usage_MB
  - container_fs_reads_MB./dev/vda, container_fs_writes_MB./dev/vda
  - container_network_transmit_packets_dropped.eth0, container_network_receive_errors.eth0

For each level, compare each component's fault-window values against its baseline (pre-fault) and against peers.

IMPORTANT about node-pod mapping:
  - Pod metric cmdb_id format is "node-X.pod-name". If multiple pods on the SAME node are anomalous, the root cause is likely the NODE.
  - Container network faults may appear at the service level — check if ALL pods of a service are affected.

**Step 1b — Filter outliers**
Remove false positives: transient spikes (<2 min), consistently high/low baselines, and cyclical patterns.

**Step 2 — Determine the root cause LEVEL (node vs pod vs service)**
  - Node-level KPI anomaly → NODE root cause (e.g., "node-4")
  - Single pod anomaly, same-node peers fine → POD root cause (e.g., "frontend-1")
  - All pods of a service anomalous → SERVICE root cause (e.g., "frontend")

**Step 3 — Identify the failure reason**
Match the anomalous KPI to the specific failure reason from the possible root causes list.
If needed, inspect `logs.csv` for supporting clues such as restarts, shutdowns, connection errors,
readiness/liveness failures, or network-policy/routing-related messages, especially for suspected
container process termination or traffic-isolation cases.

**Step 4 — Pinpoint the exact occurrence time**
Use `execute()` to find the exact timestamp. Report "YYYY-MM-DD HH:MM:SS".

**Step 5 — Submit your conclusion**
Submit the root cause component, reason, and occurrence time. Do NOT delegate conclusion to `execute()`.\
"""

VISION_CRITIC_MARKET = """\
You are a time-series graph analysis expert for a Kubernetes-based microservice system.
Examine the attached peer comparison chart and identify outlier components.

## Component naming
- Node metrics: cmdb_id = node-1, node-2, ..., node-6
- Pod/Container metrics: cmdb_id = node-X.pod-name (e.g., node-5.frontend-1)
  The "node-X" prefix tells you which physical node hosts the pod.
  If MULTIPLE pods on the SAME node show anomalies, the node itself may be the root cause.

## Rules
1. An outlier must show a visible CHANGE within the time window — \
a sudden spike, sharp drop, or clear shift from its own baseline. \
The anomaly is about BEHAVIOR CHANGE, not absolute level.
2. A component that is consistently high or low throughout the ENTIRE \
window is NOT an outlier — that is its normal baseline. Only flag it \
if it CHANGES (e.g., suddenly rises, drops, or diverges mid-window).
3. Regular repeating or cyclical patterns are NORMAL — do NOT flag them.
4. Minor fluctuations within the normal range are NOT outliers. \
Only flag CLEAR, OBVIOUS deviations visible at a glance.
5. It is perfectly fine to report NO outliers. Do not force-fit.

## Fault pattern reference (Market cloudbed system)
- Node CPU fault: system.cpu.pct_usage sustained spike on a specific node
- Node memory fault: system.mem.pct_usage sustained rise on a specific node
- Node disk I/O fault: system.io.util spike or system.io.r_s/w_s spike on a node
- Node disk space fault: system.disk.pct_usage rises toward 100%
- Node network fault: ping.can_connect drops, system.net.tcp.retrans_segs spikes
- Container CPU fault: container_cpu_usage_seconds sustained spike on specific pod(s)
- Container memory fault: container_memory_usage_MB sustained rise on specific pod(s)
- Container I/O fault: container_fs_reads_MB or container_fs_writes_MB spike
- Container network fault: container_network_*_packets_dropped or *_errors spike
- Container process termination: container_processes sudden drop

## Output format
Return ONLY a JSON object (no extra text):
{
  "outliers": [
    {"component": "<name>", "change": "<what changed and when>", "severity": "high|medium"},
    ...
  ]
}
If no outlier is found:
{"outliers": []}
"""
