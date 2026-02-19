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

schema = f"""## TELEMETRY DATA ACCESS:

- Use `telemetry.get_logs()`, `telemetry.get_metrics()`, `telemetry.get_traces()` to fetch data.
- Each returns a directory path. Read CSVs from there (e.g., `pd.read_csv(f"{{dir}}/metrics.csv")`).

## DATA SCHEMA

1.  **Metric columns** (in metrics.csv):

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
        ```

2.  **Trace columns** (in traces.csv):

    ```csv
    timestamp,cmdb_id,span_id,trace_id,duration,type,status_code,operation_name,parent_span
    1647705600361,frontend-0,a652d4d10e9478fc,9451fd8fdf746a80687451dae4c4e984,49877,rpc,0,hipstershop.CheckoutService/PlaceOrder,952754a738a11675
    ```

3.  **Log columns** (in logs.csv):

    - Proxy logs:
        ```csv
        log_id,timestamp,cmdb_id,log_name,value
        KN43pn8BmS57GQLkQUdP,1647761110,cartservice-1,log_cartservice-service_application,...
        ```

    - Service logs:
        ```csv
        log_id,timestamp,cmdb_id,log_name,value
        GIvpon8BDiVcQfZwJ5a9,1647705660,currencyservice-0,log_currencyservice-service_application,...
        ```

{cand}

## CLARIFICATION OF TELEMETRY DATA:

1. This microservice system is a E-commerce platform which includes a failover mechanism, with each service deployed across four pods. In this system, a container (pod) can be deployed in different nodes. If the root cause component is a single pod of a specific service (e.g., node-1.adservice-0), the failure may not significantly impact the corresponding service metrics. In contrast, if the root cause component is a service itself (e.g., adservice), which means all pods of this service are faulty, the corresponding service metrics will be significantly impacted. Note that `Pod` equals to `Container` in this system.

2. The service metrics only contain four KPIs: rr, sr, mrt, and count. In contrast, other metric files record a variety of KPIs. The specific names of these KPIs can be found in the `kpi_name` field.

3. Note that the `cmdb_id` is the name of specific components, including nodes, pods, services, etc.

-  Metrics:
    -  Runtime: The application name and port, e.g., `adservice.ts:8088`
    -  Service: The service name and protocol, e.g., `adservice-grpc`
    -  Container: The pod name combined with a node name, e.g., `node-1.adservice-0`
    -  Node: The node name, e.g., `node-1`
    -  Mesh: The service-to-service connection identifier within the mesh, e.g., `cartservice-1.source.cartservice.redis-cart`

-  Traces: The pod name, e.g., `adservice-0`

-  Logs: The pod name, e.g., `adservice-0`

4. In different telemetry files, the timestamp units and cmdb_id formats may vary:

- Metric: Timestamp units are in seconds (e.g., 1647781200).
- Trace: Timestamp units are in milliseconds (e.g., 1647705600361).
- Log: Timestamp units are in seconds (e.g., 1647705660).

5. Please use the UTC+8 time zone in all analysis steps since system is deployed in China/Hong Kong/Singapore."""
