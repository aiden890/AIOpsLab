# Failure Analysis: telecom-test-v6 / task_2-2

- **Log**: `results/static_problems/openrca_telecom/kg-rca/gpt-4.1-mini/telecom-test-v6/20260303_1053_task_2-2.log`
- **Executor Notebook**: `results/static_problems/openrca_telecom/kg-rca/gpt-4.1-mini/telecom-test-v6/task_2-2_executor.ipynb`
- **Agent Prompt**: `clients/openrca_rca/kg_rca_agent.py` (`SYSTEM_TEMPLATE`)
- **Runner**: `clients/run_kg_rca.py`
- **Telecom Prompt**: `clients/openrca_rca/prompts/basic_prompt_telecom.py`

---

## Result

| Item | Value |
|------|-------|
| **Ground Truth** | `db_007`, `db connection limit` |
| **Agent Prediction** | `os_003`, `CPU fault` |
| **Score** | 0.0 |
| **Steps** | 10 |

---

## Root Cause of Failure: Executor의 db_007 데이터 조회 실패

**Step 3**이 전체 실패의 결정적 원인이다.

Agent가 "db_007과 db_009의 connection limit, error rate 분석"을 요청했을 때, Executor가 생성한 코드:

```python
# cell-2 (task_2-2_executor.ipynb)
db_metrics = metric_df[metric_df['cmdb_id'].isin(['db_007', 'db_009'])].copy()
conn_limit_metrics = db_metrics[db_metrics['name'].str.contains(
    'connected_clients|connection_limit', case=False, na=False)]
```

**결과: Empty DataFrame (0 rows)**

`connected_clients` 메트릭이 `db_007`이 아닌 middleware 컴포넌트(예: `redis_*`)의 `cmdb_id`로 저장되어 있을 수 있다. Executor는 `cmdb_id='db_007'`에서만 검색했기 때문에 아무것도 찾지 못했다. **db_007에 어떤 메트릭이 존재하는지 먼저 확인하는 코드를 생성하지 않았다.**

---

## 연쇄 실패 과정

| Step | 행동 | 문제점 |
|------|------|--------|
| **3** | db_007 메트릭 조회 → Empty | Executor가 `connected_clients`만 검색. db_007의 실제 메트릭 목록 확인 안 함 |
| **4** | docker_004↔db_007 네트워크 메트릭 → Empty | `delay\|latency\|loss` 키워드로만 검색. 실제 존재하는 메트릭명 확인 안 함 |
| **5** | db_003 분석 → Empty | db_003은 KG에서 docker_005~008이 연결하는 서비스라 이 문제와 관련 없음 |
| **6** | OS 전체 CPU 분석 → 135개 "anomaly" | mean±2σ 기준으로 통계적 잡음을 이상치로 판정. os_003 CPU_idle_pct 변동이 90.63~90.78 (0.15% 차이)인데 anomaly로 분류 |
| **7** | docker_004 호스트 OS 식별 시도 → 실패 | trace 데이터에 os_* 컴포넌트가 없어서 당연히 실패 |
| **9** | 시간대 겹침으로 "correlation" 주장 | 모든 컴포넌트가 같은 시간대에 데이터가 있으니 당연히 겹침. 인과관계가 아님 |

---

## Agent가 놓친 결정적 단서

### 1. KG가 이미 답을 가리키고 있었음

```
#1  docker_004 → db_007   p95/p50=22.33x   score=110.23  ★★★
#2  docker_003 → db_009   p95/p50=1.00x    score=5.79    ★★★
```

`docker_004 → db_007`의 anomaly score가 **110.23**으로 다른 edge(5~6점)와 비교해 압도적이다. 이것은 db_007에 심각한 문제가 있다는 강한 신호였다.

### 2. Trace failure 패턴 미확인

`success=False`인 trace call 수를 한 번도 확인하지 않았다. `db connection limit`의 핵심 증거는 db_007로의 호출이 대량 실패하는 패턴인데, 이를 조회하지 않았다.

### 3. db_007 데이터가 비었을 때 포기

Empty 결과가 나왔을 때 "db_007에 어떤 메트릭이 있는지"를 확인하는 대신 바로 다른 컴포넌트로 넘어갔다.

---

## 요약

| 구분 | 내용 |
|------|------|
| **근본 원인** | Executor가 db_007 메트릭을 좁은 키워드(`connected_clients`)로만 검색해 데이터를 못 찾음 |
| **Agent 판단 오류** | 데이터 없음 → db_007 정상이라고 잘못 판단 → OS 통계적 잡음을 CPU fault로 오인 |
| **놓친 핵심** | trace의 `success=False` 패턴 분석, db_007 실제 가용 메트릭 확인 |
