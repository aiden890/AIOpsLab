# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""prompt templates to share API documentation and instructions with clients"""

# standard documentation and apis template

DOCS = """SERVICE MONITORING TASK

{prob_desc}

Available monitoring APIs:

{telemetry_apis}

Diagnostic interface:

{shell_api}

Submission API:

{submit_api}

At each turn, respond with:
Thought: <your analysis>
Action: <your next step>
"""


### custom templates

DOCS_WITH_POSSIBLE_ROOT_CAUSES = """SERVICE MONITORING TASK

{prob_desc}

Available monitoring APIs:

{telemetry_apis}

Diagnostic interface:

{shell_api}

Submission API:

{submit_api}

{possible_root_causes}

At each turn, you should respond with:
Thought: <your analysis>
Action: <your next step>
"""

DOCS_SHELL_ONLY = """{prob_desc}
You are provided with a diagnostic interface for the service:

{shell_api}

Finally, you will submit your solution for this task using the following API:

{submit_api}

IMPORTANT:
1. The submit() call must strictly follow its defined parameter signature for this task.
2. Provide the call in a markdown code block.

At each turn respond with:
Action: <your action>
"""

DOCS_TT_RCA = """ROOT CAUSE ANALYSIS — TRAIN TICKET SYSTEM

{prob_desc}

You are a Site Reliability Engineer (SRE) investigating a production incident.
No hints about the affected service or fault type are given — identify everything from the telemetry.

Available monitoring APIs:

{telemetry_apis}

Diagnostic interface:

{shell_api}

Submission API:

{submit_api}

IMPORTANT — execute() usage:
execute() takes a NATURAL LANGUAGE instruction. Do NOT write Python code inside it.
A separate Executor LLM will write and run the code for you.

CSV column formats:

Metrics CSV (from get_metrics):
  Resource metrics:  {{service}}_cpu, {{service}}_mem, {{service}}_diskio, {{service}}_socket
  Latency metrics:   {{service}}_latency-50, {{service}}_latency-90
  Error/workload:    {{service}}_error, {{service}}_workload
  Log count:         {{service}}_logcount
  Trace latency:     lat_{{service}}_{{operation}}   (per-operation latency from traces)
  Trace error rate:  err_{{service}}_{{operation}}   (per-operation error rate from traces)

Traces CSV (from get_traces):
  time, traceID, spanID, serviceName, methodName, operationName,
  startTimeMillis (ms), startTime (us), duration (us), statusCode, parentSpanID

Logs CSV (from get_logs):
  time, timestamp (ns), container_name, message, level, req_path, error
  NOTE: The "level" and "error" columns are usually EMPTY (NaN).
  Log level (INFO/WARN/ERROR) is embedded inside the "message" text, e.g.:
    "2024-01-22 10:59:05.429  INFO 1 --- [thread] class : message content"
  To filter errors, search the "message" column for "ERROR" or "Exception" strings.

Valid root cause reasons (use EXACTLY one of these strings):
  cpu stress | memory stress | network delay | packet loss | disk I/O stress | socket exhaustion

=== Investigation workflow ===

1. Use execute() to load and analyze the metrics CSV. Find anomalous services.
   IMPORTANT: Analyze EACH metric type separately — do NOT rank them on one combined score.
   Run separate anomaly detection for each group:
   - _cpu columns (%)
   - _mem columns (bytes — values in millions/billions)
   - _diskio columns (bytes — values in millions/billions)
   - _socket columns (counts)
   - _latency-50, _latency-90 columns (ms)
   - lat_* columns (trace latency, ms)
   - _error, err_* columns (counts/rates)
2. Among the anomalous services, determine which one is the root cause
   (not a victim of cascading failure). Consider temporal ordering and trace data.
   - If a service shows ONLY latency anomalies (no cpu/mem/disk/socket spike),
     the fault is likely "network delay" or "packet loss".
   - If many services show memory/cpu spikes but one service shows an earlier latency spike,
     that service is likely the root cause (network delay causing cascading resource stress).
   - If one service has a massive diskio spike while others show cpu/mem stress,
     that service is the root cause ("disk I/O stress") and others are victims.
3. Identify the fault type from the dominant anomalous metric of the ROOT CAUSE service.
4. Submit your findings:

```
submit({{"1": {{"root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS", "root cause component": "ts-service-name", "root cause reason": "fault type"}}}})
```

All three fields required:
  "root cause occurrence datetime": exact UTC time when the anomaly first appeared
  "root cause component": exact service name from telemetry
  "root cause reason": exactly one of the 6 valid reasons above

At each turn, respond with:
Thought: <your analysis of the previous output>
Action:
```
<api_call(args)>
```
"""


AUTOGEN_DOCS = """{prob_desc}
You are provided with the following APIs to interact with the service:

{telemetry_apis}

You also have access to a diagnostic interface for the service:

{shell_api}

Finally, you will submit your solution for this task using the following API:

{submit_api}

Collaborate with your team to analyze the problem and suggest appropriate API calls.
Suggest API calls in the specified format within markdown code blocks.
"""
