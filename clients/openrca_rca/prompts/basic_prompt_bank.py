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

schema = f"""## TELEMETRY DATA ACCESS:

- Use `telemetry.get_logs()`, `telemetry.get_metrics()`, `telemetry.get_traces()` to fetch data.
- Each returns a directory path. Read CSVs from there (e.g., `pd.read_csv(f"{{dir}}/metrics.csv")`).

## DATA SCHEMA

1.  **Metric columns** (in metrics.csv):

    - Container metrics:
        ```csv
        timestamp,cmdb_id,kpi_name,value
        1614787200,Tomcat04,OSLinux-CPU_CPU_CPUCpuUtil,26.2957
        ```

    - App metrics:
        ```csv
        timestamp,rr,sr,cnt,mrt,tc
        1614787440,100.0,100.0,22,53.27,ServiceTest1
        ```

2.  **Trace columns** (in traces.csv):

    ```csv
    timestamp,cmdb_id,parent_id,span_id,trace_id,duration
    1614787199628,dockerA2,369-bcou-dle-way1-c514cf30-43410@0824-2f0e47a816-17492,21030300016145905763,gw0120210304000517192504,19
    ```

3.  **Log columns** (in logs.csv):

    ```csv
    log_id,timestamp,cmdb_id,log_name,value
    8c7f5908ed126abdd0de6dbdd739715c,1614787201,Tomcat01,gc,"3748789.580: [GC (CMS Initial Mark) ..."
    ```

{cand}

## CLARIFICATION OF TELEMETRY DATA:

1. This microservice system is a banking platform.

2. The app metrics only contain four KPIs: rr, sr, cnt, and mrt. In contrast, container metrics record a variety of KPIs such as CPU usage and memory usage. The specific names of these KPIs can be found in the `kpi_name` field.

3. In different telemetry files, the timestamp units may vary:

- Metric: Timestamp units are in seconds (e.g., 1614787440).
- Trace: Timestamp units are in milliseconds (e.g., 1614787199628).
- Log: Timestamp units are in seconds (e.g., 1614787201).

4. Please use the UTC+8 time zone in all analysis steps since system is deployed in China/Hong Kong/Singapore."""
