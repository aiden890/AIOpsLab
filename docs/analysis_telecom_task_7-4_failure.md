# Failure Analysis: telecom-test-v6 / task_7-4

- **Log**: `results/static_problems/openrca_telecom/kg-rca/gpt-4.1-mini/telecom-test-v6/20260303_1057_task_7-4.log`
- **Executor Notebook**: `results/static_problems/openrca_telecom/kg-rca/gpt-4.1-mini/telecom-test-v6/task_7-4_executor.ipynb`
- **Agent Prompt**: `clients/openrca_rca/kg_rca_agent.py` (`SYSTEM_TEMPLATE`)
- **Runner**: `clients/run_kg_rca.py`

---

## Result

| Item | Value |
|------|-------|
| **Ground Truth** | `docker_008`, `CPU fault`, `2020-04-10 20:40:00` |
| **Agent Prediction** | `db_009`, `db connection limit`, `2020-04-10 20:30:00` |
| **Score** | 0.0 (component, reason, datetime 모두 틀림) |
| **Steps** | 9 |

---

## Root Cause of Failure: Agent의 Unix Timestamp 변환 오류

이번 실패의 근본 원인은 **코드 생성 문제가 아니라, Agent가 사건 시간을 잘못된 Unix timestamp으로 변환**한 것이다.

### Timestamp 비교

| 항목 | Agent가 사용한 값 | 실제 올바른 값 |
|------|-------------------|---------------|
| 20:30 UTC | `1586541000` (= **17:50 UTC**, 2시간 40분 오차) | `1586550600` |
| 21:00 UTC | `1586542800` (= **18:20 UTC**, 2시간 40분 오차) | `1586552400` |

Agent가 Step 1에서 `get_metrics("static-telecom", 1586541000, 1586542800)`를 호출했는데, 이 timestamp은 실제로 17:50~18:20 UTC 구간이다. 메트릭 데이터의 실제 timestamp은 20:00~21:00 UTC 대역(`1586548800`~)에 분포하므로, 이후 모든 시간 필터링이 빈 결과를 반환했다.

---

## 연쇄 실패 과정

| Step | 행동 | 결과 | 문제점 |
|------|------|------|--------|
| **1** | `get_metrics("static-telecom", 1586541000, 1586542800)` | 99,372 rows CSV 반환 | API는 전체 데이터를 반환했지만, Agent의 timestamp이 이미 틀림 |
| **2** | `read_metrics(...)` 200줄 읽기 | osb_001 데이터 확인 (timestamp ~1586548800) | Agent가 데이터의 실제 timestamp(20:00 UTC)과 자신의 필터(17:50 UTC)의 불일치를 눈치채지 못함 |
| **3** | Executor: db_009 메트릭 필터 → 실패 | Executor 자체 실패 | instruction이 모호해서 Executor가 코드 생성 실패 |
| **4** | Executor: db_009 CPU/avg_time/succee_rate | **All NaN** | 잘못된 timestamp 범위로 필터링 → 데이터 0건 |
| **5** | Executor: db_007, db_003, docker_001~004 | **All NaN** | 동일한 timestamp 오류 → 모든 컴포넌트 데이터 0건 |
| **8** | Executor: os_001~022 메트릭 | **Empty DataFrame** | 동일한 timestamp 오류 |
| **9** | 증거 없이 KG 힌트만으로 submit | db_009, db connection limit | 모든 메트릭 조회가 empty였으므로 KG의 "inward_spike" 패턴만 보고 추측 |

---

## Executor 코드 상세 분석

### cell-4 (Step 2, db_009 분석)

```python
start_ts = 1586541000  # Agent가 넘긴 잘못된 timestamp (실제 17:50 UTC)
end_ts = 1586542800    # 실제 18:20 UTC

# db_009의 service metrics 필터링
other_metrics = metric_df[metric_df['cmdb_id'] == 'db_009']
other_metrics_time_filtered = other_metrics[
    (other_metrics['timestamp'] >= start_ts) & (other_metrics['timestamp'] <= end_ts)
]
# → 결과: 0 rows (실제 데이터 timestamp은 ~1586548800 = 20:00 UTC)
```

db_009의 메트릭 데이터 자체는 존재하지만 (`CPU_free_pct`, `CPU_Used_Pct` 메트릭명이 감지됨), **timestamp 필터에 의해 전부 걸러짐**.

### cell-8 (Step 4, OS 컴포넌트 분석)

```python
os_metrics_time_filtered = os_metrics[
    (os_metrics['timestamp'] >= start_ts) & (os_metrics['timestamp'] <= end_ts)
]
# → Empty DataFrame (동일 원인)
```

99,372 rows 중 해당 시간대에 속하는 row가 0건.

---

## Agent가 놓친 결정적 단서

### 1. 데이터가 있는데 비어있다는 모순을 무시

Step 2에서 raw read로 99,372줄의 메트릭 데이터를 확인했다. 그런데 이후 모든 컴포넌트(db_009, db_007, docker_001~004, os_001~022)의 시간 필터링 결과가 전부 empty였다. Agent는 "데이터가 없다"고 결론지었지만, **99K줄의 데이터가 있는데 모든 컴포넌트에 대해 0건이면 시간 필터가 잘못됐다는 신호**이다.

### 2. docker_008의 CPU 메트릭 미확인

Ground truth는 `docker_008`의 `CPU fault`이다. docker_008은 KG에서 `docker_008 → db_003` 경로에 있지만 Top-5 anomalous edges에 포함되지 않았다. Agent는 KG candidate인 db_009에만 집중하고, docker_005~008(db_003 연결 그룹)의 `container_cpu_used` 메트릭을 전혀 확인하지 않았다.

### 3. KG 힌트에 과도하게 의존

모든 메트릭 조회가 empty였음에도 불구하고, KG의 "inward_spike on db_009" 패턴만 보고 "db connection limit"이라 추측하여 submit했다. 증거 없이 추론만으로 제출한 것이다.

---

## 요약

| 구분 | 내용 |
|------|------|
| **근본 원인** | Agent가 "20:30 UTC"를 `1586541000` (실제 17:50 UTC)으로 잘못 변환 → 모든 시간 필터링 실패 |
| **코드 품질** | Executor 코드 자체는 정상이나, 입력된 timestamp이 잘못되어 전부 empty 결과 |
| **Agent 판단 오류** | 99K rows 데이터에서 모든 컴포넌트가 empty인 이상 신호를 무시 → KG 힌트만으로 추측 제출 |
| **놓친 핵심** | docker_008의 `container_cpu_used` 메트릭 확인, timestamp 변환 오류 디버깅 |

---

## task_2-2와의 비교

| 항목 | task_2-2 | task_7-4 |
|------|----------|----------|
| **실패 유형** | Executor의 좁은 키워드 검색 | Agent의 timestamp 변환 오류 |
| **데이터 접근** | 일부 성공, 일부 empty | 전부 empty |
| **근본 원인** | 코드 생성 품질 | Agent 추론 오류 (시간 변환) |
| **공통점** | empty 결과를 "데이터 없음"으로 오판, KG에 과도 의존 |
