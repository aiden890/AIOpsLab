## AIOpsLab Telemetry API 요약

### 1. PrometheusAPI (메트릭)
**파일:** `aiopslab/observer/metric_api.py`

```python
PrometheusAPI(url: str, namespace: str)
```

| 메서드 | 파라미터 | 리턴 |
|--------|----------|------|
| `query_range()` | metric_name, pod, start_time, end_time, namespace, step | `[{"time": datetime, "value": float}]` |
| `export_all_metrics()` | start_time, end_time, save_path, step | CSV 경로 문자열 |
| `get_all_metrics()` | - | 메트릭 이름 리스트 |

---

### 2. TraceAPI (트레이스)
**파일:** `aiopslab/observer/trace_api.py`

```python
TraceAPI(namespace: str)
```

| 메서드 | 파라미터 | 리턴 |
|--------|----------|------|
| `get_services()` | - | 서비스 이름 리스트 |
| `get_traces()` | service_name, start_time, end_time, limit | Raw trace JSON |
| `process_traces()` | traces | DataFrame (trace_id, span_id, parent_span, service_name, operation_name, start_time, duration, has_error, response) |

---

### 3. LogAPI (로그)
**파일:** `aiopslab/observer/log_api.py`

```python
LogAPI(url: str, username: str, password: str)
```

| 메서드 | 파라미터 | 리턴 |
|--------|----------|------|
| `log_extract()` | start_time, end_time, path | CSV 저장 |
| `query()` | start_time, end_time | ES hits 리스트 |

**CSV 컬럼:** log_id, timestamp, date, pod_name, container_name, namespace, node_name, message

---

### 시간 파라미터
`int` (Unix timestamp) | `datetime` | `str` (ISO 8601)

### 설정 파일
`aiopslab/observer/monitor_config.yaml`
