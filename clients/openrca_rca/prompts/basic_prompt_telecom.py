cand = """## POSSIBLE ROOT CAUSE REASONS:

- CPU fault
- network delay
- network loss
- db connection limit
- db close

## POSSIBLE ROOT CAUSE COMPONENTS:

(if the root cause is at the node level, i.e., the root cause is a specific node)

- os_001
- os_002
- os_003
- os_004
- os_005
- os_006
- os_007
- os_008
- os_009
- os_010
- os_011
- os_012
- os_013
- os_014
- os_015
- os_016
- os_017
- os_018
- os_019
- os_020
- os_021
- os_022

(if the root cause is at the pod level, i.e., the root cause is a specific container)

- docker_001
- docker_002
- docker_003
- docker_004
- docker_005
- docker_006
- docker_007
- docker_008

(if the root cause is at the service level, i.e., if all pods of a specific service are faulty, the root cause is the service itself)

- db_001
- db_002
- db_003
- db_004
- db_005
- db_006
- db_007
- db_008
- db_009
- db_010
- db_011
- db_012
- db_013"""

# ---------------------------------------------------------------------------
# Schema sections (assembled by build_schema)
# ---------------------------------------------------------------------------

_METRIC_SCHEMA = """\
**Metric columns** (in metrics.csv):

    - App metrics:
        ```csv
        serviceName,startTime,avg_time,num,succee_num,succee_rate
        osb_001,1586534400.0,0.333,1,1,1.0
        ```

    - Container metrics:
        ```csv
        itemid,name,bomc_id,timestamp,value,cmdb_id
        999999996381330,container_mem_used,ZJ-004-060,1586534423.0,59.000000,docker_008
        ```

    - Middleware metrics:
        ```csv
        itemid,name,bomc_id,timestamp,value,cmdb_id
        999999996508323,connected_clients,ZJ-005-024,1586534672.0,25,redis_003
        ```

    - Node metrics:
        ```csv
        itemid,name,bomc_id,timestamp,value,cmdb_id
        999999996487783,CPU_iowait_time,ZJ-001-010,1586534683.0,0.022954,os_017
        ```

    - Service metrics:
        ```csv
        itemid,name,bomc_id,timestamp,value,cmdb_id
        999999998650974,MEM_Total,ZJ-002-055,1586534694.0,381.902264,db_003
        ```"""

_TRACE_SCHEMA = """\
**Trace columns** (in traces.csv):

    ```csv
    callType,startTime,elapsedTime,success,traceId,id,pid,cmdb_id,dsName,serviceName
    JDBC,1586534400.335,2.0,True,01df517164d1c0365586,407d617164d1c14f2613,6e02217164d1c14b2607,docker_006,db_003,
    ```"""


def build_schema(condition="all"):
    """Build schema string with telemetry sections filtered by ablation condition.

    Args:
        condition: "all", "no_log", "no_metric", or "no_trace"
    """
    # Telecom dataset has NO log data (only metric + trace)
    enable_log = False
    enable_metric = condition != "no_metric"
    enable_trace = condition != "no_trace"

    # 1. Telemetry access
    funcs = []
    if enable_log:
        funcs.append("`telemetry.get_logs()`")
    if enable_metric:
        funcs.append("`telemetry.get_metrics()`")
    if enable_trace:
        funcs.append("`telemetry.get_traces()`")

    # Use first available function as example
    example_func = funcs[0].strip("`") if funcs else "telemetry.get_metrics()"

    sections = []
    sections.append(
        f"## TELEMETRY DATA ACCESS:\n\n"
        f"- Use {', '.join(funcs)} to fetch data.\n"
        f"- Each returns a file path to a CSV. Read it directly "
        f"(e.g., `pd.read_csv({example_func})`)."
    )

    # 2. Data schema (telecom has no log schema)
    data_items = []
    n = 1
    if enable_metric:
        data_items.append(f"{n}.  {_METRIC_SCHEMA}")
        n += 1
    if enable_trace:
        data_items.append(f"{n}.  {_TRACE_SCHEMA}")
        n += 1
    if data_items:
        sections.append("## DATA SCHEMA\n\n" + "\n\n".join(data_items))

    # 3. Candidates
    sections.append(cand)

    # 4. Clarification
    cl = []
    cn = 1
    cl.append(f"## CLARIFICATION OF TELEMETRY DATA:\n\n"
              f"{cn}. This service system is a telecom database system.")
    cn += 1

    if enable_metric:
        cl.append(
            f"\n\n{cn}. The `metric_app` data only contains five KPIs: startTime, avg_time, "
            f"num, succee_num, succee_rate. In contrast, other metrics record a variety of "
            f"KPIs, such as CPU usage and memory usage. The specific names of these KPIs "
            f"can be found in the `name` field.")
        cn += 1

    timestamps = []
    if enable_metric:
        timestamps.append("- Metric: Timestamp units are in seconds (e.g., 1586534423.0).")
    if enable_trace:
        timestamps.append("- Trace: Timestamp units are in seconds (e.g., 1586534400.335).")
    if timestamps:
        cl.append(
            f"\n\n{cn}. All telemetry timestamps are in **seconds** (Unix epoch). "
            f"Use `pd.to_datetime(ts, unit='s')` for conversion:\n\n"
            + "\n".join(timestamps))
        cn += 1

    # Done: Changed from UTC+8 to UTC
    # cl.append(
    #     f"\n\n{cn}. Please use the UTC+8 time zone in all analysis steps "
    #     f"since system is deployed in China/Hong Kong/Singapore.")
    cl.append(
        f"\n\n{cn}. Please use the UTC time zone in all analysis steps ")

    sections.append("".join(cl))

    return "\n\n".join(sections)


# Default schema (all types) for backward compatibility
schema = build_schema("all")

guidance = """\
## TELECOM-SPECIFIC RCA GUIDANCE:

Since logs are unavailable, use metrics and traces to infer root cause reason:

- **CPU fault**: High `cpu_used` (container metric) or high CPU-related KPIs (e.g., `CPU_iowait_time`, `CPU_user_time`) for the faulty component.
- **network delay**: High `elapsedTime` in trace spans to/from the faulty component, or high network latency KPIs in node metrics.
- **network loss**: High packet-drop KPIs (`net_if_in_drop`, `net_if_out_drop`) in node metrics, or sudden drops in `succee_rate` in app metrics.
- **db connection limit**: High `connected_clients` in middleware metrics, or many failed trace calls (success=False) to a db service with no gap between calls.
- **db close**: Sudden complete failure of all trace calls to a db service (all success=False) with a sharp drop to 0 in that service's metrics."""
