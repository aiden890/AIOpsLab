cand = """## POSSIBLE ROOT CAUSE COMPONENTS:

(if the root cause is at the node level, i.e., the root cause is a specific node)
- node-1
- node-2
- node-3
- node-4
- node-5
- node-6

(if the root cause is at the pod level, i.e., the root cause is a specific container)

- frontend-0
- frontend-1
- frontend-2
- frontend2-0
- shippingservice-0
- shippingservice-1
- shippingservice-2
- shippingservice2-0
- checkoutservice-0
- checkoutservice-1
- checkoutservice-2
- checkoutservice2-0
- currencyservice-0
- currencyservice-1
- currencyservice-2
- currencyservice2-0
- adservice-0
- adservice-1
- adservice-2
- adservice2-0
- emailservice-0
- emailservice-1
- emailservice-2
- emailservice2-0
- cartservice-0
- cartservice-1
- cartservice-2
- cartservice2-0
- productcatalogservice-0
- productcatalogservice-1
- productcatalogservice-2
- productcatalogservice2-0
- recommendationservice-0
- recommendationservice-1
- recommendationservice-2
- recommendationservice2-0
- paymentservice-0
- paymentservice-1
- paymentservice-2
- paymentservice2-0

(if the root cause is at the service level, i.e., if all pods of a specific service are faulty, the root cause is the service itself)

- frontend
- shippingservice
- checkoutservice
- currencyservice
- adservice
- emailservice
- cartservice
- productcatalogservice
- recommendationservice
- paymentservice

## POSSIBLE ROOT CAUSE REASONS:

- container CPU load
- container memory load
- container network packet retransmission
- container network packet corruption
- container network latency
- container packet loss
- container process termination
- container read I/O load
- container write I/O load
- node CPU load
- node CPU spike
- node memory consumption
- node disk read I/O consumption
- node disk write I/O consumption
- node disk space consumption"""

# ---------------------------------------------------------------------------
# Schema sections (assembled by build_schema)
# ---------------------------------------------------------------------------

_METRIC_SCHEMA = """\
**Metric columns** (in metrics.csv):

    - Container metrics:
        ```csv
        timestamp,cmdb_id,kpi_name,value
        1647781200,node-6.adservice2-0,container_fs_writes_MB./dev/vda,0.0
        ```

    - Mesh metrics:
        ```csv
        timestamp,cmdb_id,kpi_name,value
        1647790380,cartservice-1.source.cartservice.redis-cart,istio_tcp_sent_bytes.-,1255.0
        ```

    - Node metrics:
        ```csv
        timestamp,cmdb_id,kpi_name,value
        1647705600,node-1,system.cpu.iowait,0.31
        ```

    - Runtime metrics:
        ```csv
        timestamp,cmdb_id,kpi_name,value
        1647730800,adservice.ts:8088,java_nio_BufferPool_TotalCapacity.direct,57343.0
        ```

    - Service metrics:
        ```csv
        service,timestamp,rr,sr,mrt,count
        adservice-grpc,1647716400,100.0,100.0,2.429508196728182,61
        ```"""

_TRACE_SCHEMA = """\
**Trace columns** (in traces.csv):

    ```csv
    timestamp,cmdb_id,span_id,trace_id,duration,type,status_code,operation_name,parent_span
    1647705600.361,frontend-0,a652d4d10e9478fc,9451fd8fdf746a80687451dae4c4e984,49877,rpc,0,hipstershop.CheckoutService/PlaceOrder,952754a738a11675
    ```"""

_LOG_SCHEMA = """\
**Log columns** (in logs.csv):

    - Proxy logs:
        ```csv
        log_id,timestamp,cmdb_id,log_name,value
        KN43pn8BmS57GQLkQUdP,1647761110,cartservice-1,log_cartservice-service_application,...
        ```

    - Service logs:
        ```csv
        log_id,timestamp,cmdb_id,log_name,value
        GIvpon8BDiVcQfZwJ5a9,1647705660,currencyservice-0,log_currencyservice-service_application,...
        ```"""

_CMDB_METRIC = """\
-  Metrics:
    -  Runtime: The application name and port, e.g., `adservice.ts:8088`
    -  Service: The service name and protocol, e.g., `adservice-grpc`
    -  Container: The pod name combined with a node name, e.g., `node-1.adservice-0`
    -  Node: The node name, e.g., `node-1`
    -  Mesh: The service-to-service connection identifier within the mesh, e.g., `cartservice-1.source.cartservice.redis-cart`"""

_CMDB_TRACE = """-  Traces: The pod name, e.g., `adservice-0`"""

_CMDB_LOG = """-  Logs: The pod name, e.g., `adservice-0`"""


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

    # Use first available function as example
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
        f"{cn}. This microservice system is a E-commerce platform which includes a "
        f"failover mechanism, with each service deployed across four pods. In this system, "
        f"a container (pod) can be deployed in different nodes. If the root cause component "
        f"is a single pod of a specific service (e.g., node-1.adservice-0), the failure may "
        f"not significantly impact the corresponding service metrics. In contrast, if the "
        f"root cause component is a service itself (e.g., adservice), which means all pods "
        f"of this service are faulty, the corresponding service metrics will be significantly "
        f"impacted. Note that `Pod` equals to `Container` in this system.")
    cn += 1

    if enable_metric:
        cl.append(
            f"\n\n{cn}. The service metrics only contain four KPIs: rr, sr, mrt, and count. "
            f"In contrast, other metric files record a variety of KPIs. The specific names "
            f"of these KPIs can be found in the `kpi_name` field.")
        cn += 1

    # cmdb_id clarification (per-type sub-items)
    cmdb_items = []
    if enable_metric:
        cmdb_items.append(_CMDB_METRIC)
    if enable_trace:
        cmdb_items.append(_CMDB_TRACE)
    if enable_log:
        cmdb_items.append(_CMDB_LOG)
    if cmdb_items:
        cl.append(
            f"\n\n{cn}. Note that the `cmdb_id` is the name of specific components, "
            f"including nodes, pods, services, etc.\n\n" + "\n\n".join(cmdb_items))
        cn += 1

    timestamps = []
    if enable_metric:
        timestamps.append("- Metric: Timestamp units are in seconds (e.g., 1647781200).")
    if enable_trace:
        timestamps.append("- Trace: Timestamp units are in seconds (e.g., 1647705600.361).")
    if enable_log:
        timestamps.append("- Log: Timestamp units are in seconds (e.g., 1647705660).")
    if timestamps:
        cl.append(
            f"\n\n{cn}. All telemetry timestamps are in **seconds** (Unix epoch). "
            f"Use `pd.to_datetime(ts, unit='s')` for conversion:\n\n"
            + "\n".join(timestamps))
        cn += 1

    cl.append(
        f"\n\n{cn}. Please use the UTC+8 time zone in all analysis steps "
        f"since system is deployed in China/Hong Kong/Singapore.")

    sections.append("".join(cl))

    return "\n\n".join(sections)


# Default schema (all types) for backward compatibility
schema = build_schema("all")
