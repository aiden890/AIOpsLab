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

schema = f"""## TELEMETRY DATA ACCESS:

- Use `telemetry.get_metrics()`, `telemetry.get_traces()` to fetch data.
- Each returns the **full file path** to a CSV. Read it directly:
  `metric_path = telemetry.get_metrics(); df = pd.read_csv(metric_path)`

## DATA SCHEMA

1.  **Metric columns** (in metrics.csv):

    - App metrics:
        ```csv
        serviceName,startTime,avg_time,num,succee_num,succee_rate
        osb_001,1586534400000,0.333,1,1,1.0
        ```

    - Container metrics:
        ```csv
        itemid,name,bomc_id,timestamp,value,cmdb_id
        999999996381330,container_mem_used,ZJ-004-060,1586534423000,59.000000,docker_008
        ```

    - Middleware metrics:
        ```csv
        itemid,name,bomc_id,timestamp,value,cmdb_id
        999999996508323,connected_clients,ZJ-005-024,1586534672000,25,redis_003
        ```

    - Node metrics:
        ```csv
        itemid,name,bomc_id,timestamp,value,cmdb_id
        999999996487783,CPU_iowait_time,ZJ-001-010,1586534683000,0.022954,os_017
        ```

    - Service metrics:
        ```csv
        itemid,name,bomc_id,timestamp,value,cmdb_id
        999999998650974,MEM_Total,ZJ-002-055,1586534694000,381.902264,db_003
        ```

2.  **Trace columns** (in traces.csv):

    ```csv
    callType,startTime,elapsedTime,success,traceId,id,pid,cmdb_id,dsName,serviceName
    JDBC,1586534400335,2.0,True,01df517164d1c0365586,407d617164d1c14f2613,6e02217164d1c14b2607,docker_006,db_003,
    ```

{cand}

## CLARIFICATION OF TELEMETRY DATA:

1. This service system is a telecom database system.

2. The metrics CSV contains merged data from **multiple metric files** (app, container, node, service, middleware) with different column structures. Non-applicable columns will be NaN. Always filter by `cmdb_id` to get per-component metrics, and by `name` to select the right KPI:
   - App metrics: columns `serviceName, startTime, avg_time, num, succee_num, succee_rate`
   - All other metrics: columns `itemid, name, bomc_id, timestamp, value, cmdb_id`

3. In all telemetry files, the timestamp units and cmdb_id formats remain consistent:

- Metric: Timestamp units are in **seconds** (e.g., 1586534423).
- Metric (app): `startTime` is in **seconds**.
- Trace: Timestamp units are in **seconds** (e.g., 1586534400).

4. All issues use **UTC** time. However, the local machine's default timezone is unknown.

"""

guidance = """\
## TELECOM-SPECIFIC RCA GUIDANCE:

Since logs are unavailable, use metrics and traces to infer root cause reason:

- **CPU fault**: High `cpu_used` (container metric) or high CPU-related KPIs (e.g., `CPU_iowait_time`, `CPU_user_time`) for the faulty component.
- **network delay**: High `elapsedTime` in trace spans to/from the faulty component, or high network latency KPIs in node metrics.
- **network loss**: High packet-drop KPIs (`net_if_in_drop`, `net_if_out_drop`) in node metrics, or sudden drops in `succee_rate` in app metrics.
- **db connection limit**: High `connected_clients` in middleware metrics, or many failed trace calls (success=False) to a db service with no gap between calls.
- **db close**: Sudden complete failure of all trace calls to a db service (all success=False) with a sharp drop to 0 in that service's metrics."""
