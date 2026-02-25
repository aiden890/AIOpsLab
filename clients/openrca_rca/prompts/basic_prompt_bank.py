cand = """## POSSIBLE ROOT CAUSE REASONS:

- high CPU usage
- high memory usage
- network latency
- network packet loss
- high disk I/O read usage
- high disk space usage
- high JVM CPU load
- JVM Out of Memory (OOM) Heap

## POSSIBLE ROOT CAUSE COMPONENTS:

- apache01
- apache02
- Tomcat01
- Tomcat02
- Tomcat04
- Tomcat03
- MG01
- MG02
- IG01
- IG02
- Mysql01
- Mysql02
- Redis01
- Redis02"""

# ---------------------------------------------------------------------------
# Schema sections (assembled by build_schema)
# ---------------------------------------------------------------------------

_METRIC_SCHEMA = """\
**Metric columns** (in metrics.csv):

    - Container metrics:
        ```csv
        timestamp,cmdb_id,kpi_name,value
        1614787200,Tomcat04,OSLinux-CPU_CPU_CPUCpuUtil,26.2957
        ```

    - App metrics:
        ```csv
        timestamp,rr,sr,cnt,mrt,tc
        1614787440,100.0,100.0,22,53.27,ServiceTest1
        ```"""

_TRACE_SCHEMA = """\
**Trace columns** (in traces.csv):

    ```csv
    timestamp,cmdb_id,parent_id,span_id,trace_id,duration
    1614787199628,dockerA2,369-bcou-dle-way1-c514cf30-43410@0824-2f0e47a816-17492,21030300016145905763,gw0120210304000517192504,19
    ```"""

_LOG_SCHEMA = """\
**Log columns** (in logs.csv):

    ```csv
    log_id,timestamp,cmdb_id,log_name,value
    8c7f5908ed126abdd0de6dbdd739715c,1614787201,Tomcat01,gc,"3748789.580: [GC (CMS Initial Mark) ..."
    ```"""


def build_schema(condition="all"):
    """Build schema string with telemetry sections filtered by ablation condition.

    Args:
        condition: "all", "no_log", "no_metric", or "no_trace"
    """
    enable_log = condition != "no_log"
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

    example_func = funcs[0].strip("`") if funcs else "telemetry.get_metrics()"

    sections = []
    sections.append(
        f"## TELEMETRY DATA ACCESS:\n\n"
        f"- Use {', '.join(funcs)} to fetch data.\n"
        f"- Each returns a file path to a CSV. Read it directly "
        f"(e.g., `pd.read_csv({example_func})`)."
    )

    # 2. Data schema
    data_items = []
    n = 1
    if enable_metric:
        data_items.append(f"{n}.  {_METRIC_SCHEMA}")
        n += 1
    if enable_trace:
        data_items.append(f"{n}.  {_TRACE_SCHEMA}")
        n += 1
    if enable_log:
        data_items.append(f"{n}.  {_LOG_SCHEMA}")
        n += 1
    if data_items:
        sections.append("## DATA SCHEMA\n\n" + "\n\n".join(data_items))

    # 3. Candidates
    sections.append(cand)

    # 4. Clarification
    cl = []
    cn = 1
    cl.append(
        f"## CLARIFICATION OF TELEMETRY DATA:\n\n"
        f"{cn}. This microservice system is a banking platform.")
    cn += 1

    if enable_metric:
        cl.append(
            f"\n\n{cn}. The app metrics only contain four KPIs: rr, sr, cnt, and mrt. "
            f"In contrast, container metrics record a variety of KPIs such as CPU usage "
            f"and memory usage. The specific names of these KPIs can be found in the "
            f"`kpi_name` field.")
        cn += 1

    timestamps = []
    if enable_metric:
        timestamps.append("- Metric: Timestamp units are in seconds (e.g., 1614787440).")
    if enable_trace:
        timestamps.append("- Trace: Timestamp units are in milliseconds (e.g., 1614787199628).")
    if enable_log:
        timestamps.append("- Log: Timestamp units are in seconds (e.g., 1614787201).")
    if timestamps:
        cl.append(
            f"\n\n{cn}. In different telemetry files, the timestamp units may vary:\n\n"
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
