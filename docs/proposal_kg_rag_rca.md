# Proposal: Per-Incident KG + RAG for Root Cause Analysis

## 1. Overview

현재 RCA 접근법의 문제: LLM이 raw telemetry를 직접 분석하면 hallucination 발생 가능, 비효율적 탐색, 일관성 부족.

**제안**: 에러 감지 시점에 **현재 incident의 telemetry를 전처리하여 Knowledge Graph(KG)를 구축**하고, 이를 텍스트로 직렬화(serialize)하여 **RAG 형태로 LLM 프롬프트에 주입**하는 방식.

### 핵심 원칙

1. **이전 사례 학습이 아님** — KG는 현재 incident에서만 구축
2. **inject_time 미사용** — 비지도(unsupervised) 이상 탐지로 이상 구간 추정
3. **KG는 도구** — LLM의 hallucination을 방지하고 구조화된 근거를 제공. LLM은 KG를 참고하되, executor를 통해 추가 분석도 가능
4. **Phase별 테스트** — Trace KG → Metric KG → Log KG 순서로 독립 구축·검증

## 2. Target Datasets

### 2.1 Bank (CCF AIOps 2022)

```
Architecture: IG → Tomcat → MG → docker (4-tier Java middleware)
Faults:       136건, 14 컴포넌트, 8 fault 유형
Trace:        계층적 span (IG→Tomcat→MG→docker, ~47 spans/trace)
              커버리지: 12/14 (apache, MySQL, Redis 미포함)
              Topology: 28/32 edges 안정 (87.5%)
Metric:       18 컴포넌트 (container) + 11 서비스 (app)
Log:          ~2.6M rows/date, message 컬럼에 level 내장
```

### 2.2 Telecom (CCF AIOps 2022)

```
Architecture: os(node) → docker(container) → db(service) (3-tier infra)
              + redis(middleware), osb(service app)
Faults:       51건, 15 컴포넌트, 5 fault 유형
Trace:        평면 JDBC 호출 (docker→db, 1-hop)
              커버리지: 11/47 (os_*, redis_* 미포함)
              Topology: 12/12 edges 안정 (100%)
Metric:       47 컴포넌트 (container+node+service+middleware+app)
Log:          없음
```

### 2.3 주요 차이점

| 항목 | Bank | Telecom |
|------|------|---------|
| Trace 깊이 | 4-tier 계층적 | 1-hop 평면 |
| 비traced 컴포넌트 fault | isolated (Redis/MySQL/apache) | os_* 네트워크 fault 20건 |
| Log | 있음 | 없음 |
| Colocation 정보 | 없음 | bomc_id |
| Metric 분리 | container + app | 5개 파일 (container/node/service/middleware/app) |

## 3. Research Findings

### 3.1 Trace 분석 결과

**Q: Component 간 dependency graph가 시간에 따라 변하는가?**

A: **변하지 않는다.**
- Bank: 10개 date에서 28/32 edges 안정 (4개 Tomcat02 edges가 2021_03_09에만 누락)
- Telecom: 15개 date에서 12/12 edges 100% 안정
- 결론: topology는 static으로 1회 구축 가능

**Q: 변하는 것은 무엇인가?**

A: **Edge latency만 변한다.**
- Bank 예시 (Tomcat01 high CPU):
  - IG02→Tomcat01: 37.8ms → 87.4ms (2.31x)
  - Tomcat01→MG01: 7.2ms → 15.3ms (2.12x)
- 장애 컴포넌트의 incoming/outgoing edge latency가 증가

**Q: Trace로 표현 가능한 KG 유형은?**

1. **Static Topology KG** — 컴포넌트 간 호출 관계 (고정)
2. **Latency Profile KG** — edge별 baseline 통계 (고정)
3. **Per-Incident Anomaly KG** — 장애 시점 edge 이상 (변동)
4. **Network Gap KG** — parent-child latency gap (네트워크 지연 신호)
5. **Call Chain Depth KG** — 계층적 호출 깊이 (고정)

**Q: 비traced 컴포넌트는 어떻게 진단하는가?**

A: **Peer comparison으로 충분하다.**
- Redis02 CPU fault: Redis02만 10.3% (Redis01은 0.7%), 다른 서비스 정상 → isolated
- Mysql02 memory fault: Mysql02만 비정상, 서비스 latency 정상 → isolated
- apache02 network latency: TCP-FIN-WAIT 미묘한 변화, cascading 없음
- 결론: 비traced 컴포넌트는 fault가 isolated되므로, trace 없이 metric peer comparison으로 진단 가능

### 3.2 Trace가 누락된 이유

- Bank: tracing SDK가 Java middleware에만 설치 (IG, Tomcat, MG, docker). apache는 gateway로 trace를 시작하지만 자체 span을 생성하지 않음. MySQL/Redis는 instrumentation 없음
- Telecom: trace는 JDBC call만 기록 (docker→db). os_*는 infrastructure node로 application trace 대상 아님

## 4. Trace KG Design (Phase 1)

### 4.1 KG 구축 Strategy

#### Strategy A: Static Topology + Anomaly Overlay

가장 기본적인 접근. topology를 1회 구축한 뒤, per-incident anomaly를 overlay.

```
[Entity Types]
  Component(name, type)     — e.g., Component("Tomcat01", "middleware")
  Edge(caller, callee)      — e.g., Edge("IG02", "Tomcat01")

[Relation Types]
  CALLS(caller, callee, baseline_latency, baseline_count)
  ANOMALOUS(edge, current_latency, ratio, direction)

[Construction]
  1. Topology: trace에서 (parent_cmdb_id → cmdb_id) unique pairs 추출
  2. Baseline: 전체 시간 구간의 edge latency 중앙값
  3. Anomaly detection (unsupervised):
     - 이동 평균/이동 표준편차 기반 이상 구간 탐지
     - 또는 시간을 N등분하여 각 구간의 latency를 비교
  4. Anomaly overlay: 이상 구간에서 edge latency 변화 기록
```

#### Strategy B: Propagation Path KG

Bank처럼 깊은 call chain이 있을 때 유용. 장애 전파 경로를 시간 순서로 표현.

```
[Entity Types]
  Component(name)
  AnomalyEvent(component, metric, time, severity)

[Relation Types]
  CALLS(caller, callee)
  PROPAGATES_TO(source_anomaly, target_anomaly, time_delta)

[Construction]
  1. Edge별 anomaly detection (시계열 changepoint)
  2. Anomaly가 발생한 edge를 시간 순서로 정렬
  3. Caller→callee 방향으로 전파 경로 추론:
     "IG02→Tomcat01 at T1" → "Tomcat01→MG01 at T2" (T2 > T1)
```

#### Strategy C: Network Gap KG

network delay/packet loss fault 특화. parent-child 간 latency gap으로 네트워크 구간 문제 탐지.

```
[Entity Types]
  Component(name)
  NetworkSegment(caller, callee)

[Relation Types]
  HAS_GAP(segment, gap_ms, ratio_to_baseline)

[Construction]
  1. 각 trace에서 parent span duration - child span duration = network gap
  2. Edge별 network gap 통계 (baseline vs anomaly period)
  3. Gap이 비정상적으로 큰 segment를 highlight
```

#### Strategy D: Aggregated Service Health KG

trace에서 service-level health 지표를 집계. error rate, throughput 변화 등.

```
[Entity Types]
  Component(name, avg_latency, error_rate, throughput)

[Relation Types]
  CALLS(caller, callee)
  DEGRADES(component, metric, severity)

[Construction]
  1. Component별 latency 평균/P90, error count, span count 집계
  2. 시간 구간별 변화 비교 (전체 대비 후반부)
  3. 유의미한 변화가 있는 component에 DEGRADES 관계 추가
```

### 4.2 Bank vs Telecom 적용 차이

| Strategy | Bank | Telecom |
|----------|------|---------|
| A: Topology+Anomaly | 유용 (4-tier, edge latency 변화 뚜렷) | 제한적 (1-hop, 단순) |
| B: Propagation Path | **매우 유용** (전파 경로 추론 가능) | 의미 없음 (1-hop) |
| C: Network Gap | 유용 (network delay fault에 특화) | 제한적 (gap 계산 어려움) |
| D: Service Health | 유용 (baseline 비교) | 유용 (docker/db health) |

### 4.3 unsupervised 이상 구간 탐지 (inject_time 미사용)

inject_time 없이 이상 구간을 탐지하는 방법:

1. **시간 분할 비교**: 전체 데이터를 전반/후반으로 나눠 비교 (Bank 데이터는 전반에 정상, 후반에 장애가 있는 구조)
2. **이동 윈도우 z-score**: 각 edge latency 시계열에 rolling mean/std 적용, z > 3인 구간을 anomaly로 탐지
3. **Changepoint detection**: PELT, BOCPD 등으로 통계적 변환점 탐지
4. **Peer comparison**: 같은 tier의 다른 component와 비교 (Tomcat01 vs Tomcat02/03/04)

**Phase 1 초기 구현**: 방법 1 (시간 분할) + 방법 4 (peer comparison)으로 시작. 이유:
- 구현이 간단하고 Bank/Telecom 모두 적용 가능
- Bank 데이터 구조가 "전반 정상 / 후반 장애" 패턴
- 이후 changepoint detection 추가 가능

### 4.4 텍스트 직렬화 포맷 (top-K)

KG를 LLM 프롬프트에 주입하기 위한 텍스트 변환. **top-K만 포함** (전체 edge가 아닌 이상 정도 상위 K개).

#### Format 1: Structured Sections

```
=== TRACE KNOWLEDGE GRAPH ===

[Topology] (13 edges)
IG01 → Tomcat01, Tomcat02, Tomcat03
IG02 → Tomcat01, Tomcat02, Tomcat04
Tomcat01 → MG01, MG02
...

[Top-5 Anomalous Edges]
1. IG02 → Tomcat01: latency 37.8ms → 87.4ms (2.31x baseline) ★★★
2. Tomcat01 → MG01: latency 7.2ms → 15.3ms (2.12x baseline) ★★
3. IG01 → Tomcat01: latency 35.1ms → 62.3ms (1.77x baseline) ★★
4. Tomcat01 → MG02: latency 8.1ms → 13.2ms (1.63x baseline) ★
5. IG02 → Tomcat02: latency 38.2ms → 42.1ms (1.10x baseline)

[Propagation Pattern]
Anomaly first at: Tomcat01 incoming edges (T=120s)
Then propagated to: Tomcat01 outgoing edges (T=125s)
Direction: inward → outward (Tomcat01 is likely root cause)
```

#### Format 2: Compact JSON

```json
{"topology": {"IG01": ["Tomcat01","Tomcat02"], ...},
 "anomalies": [
   {"edge": "IG02→Tomcat01", "baseline": 37.8, "current": 87.4, "ratio": 2.31},
   ...
 ],
 "suspect": "Tomcat01", "pattern": "inward_spike"}
```

#### Format 3: Markdown Table

```markdown
| Rank | Edge | Baseline | Current | Ratio | Signal |
|------|------|----------|---------|-------|--------|
| 1 | IG02→Tomcat01 | 37.8ms | 87.4ms | 2.31x | ★★★ |
| 2 | Tomcat01→MG01 | 7.2ms | 15.3ms | 2.12x | ★★ |
...
```

#### Format 4: Natural Language Summary

```
The trace analysis shows Tomcat01 is the most suspicious component.
All edges INTO Tomcat01 show 1.8-2.3x latency increase, while edges
OUT of Tomcat01 show 1.6-2.1x increase. This "inward then outward"
pattern suggests Tomcat01 itself is degraded, not a downstream dependency.
Other components (Tomcat02-04, MG01/02, IG01/02) show normal latency.
```

**Phase 1 초기 구현**: Format 1 (Structured Sections)으로 시작. 이유:
- 구조화되어 있어 LLM이 파싱하기 쉬움
- top-K로 정보량 제한
- 추후 A/B 테스트로 최적 포맷 선정

### 4.5 비traced 컴포넌트 처리

Trace KG만으로는 진단 불가한 컴포넌트:
- Bank: apache01/02, MySQL01/02, Redis01/02
- Telecom: os_*, redis_*

**Phase 1 대응**:
- Trace KG에 `[Non-Traced Components]` 섹션 추가
- "이 컴포넌트들은 trace에 포함되지 않으므로 metric 분석이 필수" 명시
- Metric KG (Phase 2)에서 peer comparison으로 보완

## 5. Metric KG Design (Phase 2, outline)

### 5.1 구축 Strategy (후보)

1. **Z-score Anomaly KG**: 각 KPI의 시계열에서 z-score 기반 이상 탐지
2. **Peer Comparison KG**: 같은 유형 컴포넌트 간 비교 (Redis01 vs Redis02)
3. **Cross-metric Correlation KG**: CPU↑ + latency↑ 등 metric 간 상관관계
4. **Temporal Pattern KG**: changepoint + 시간 순서로 anomaly 전파 추론

### 5.2 Bank vs Telecom

- Bank: container metric (369 KPIs) + app metric (11 services)
- Telecom: 5개 metric 파일 (9~51 KPIs/컴포넌트)
- 공통: peer comparison 가능 (같은 역할 컴포넌트 2~4개씩)

## 6. Log KG Design (Phase 3, outline)

### 6.1 Bank만 해당 (Telecom에는 log 없음)

1. **Error Burst KG**: 시간대별 ERROR/Exception 빈도
2. **Service Error Pattern KG**: 서비스별 에러 메시지 클러스터링
3. **Log-Metric Correlation KG**: 에러 시점과 metric 이상 시점 매칭

## 7. Fusion KG Design (Phase 4, outline)

Phase 1~3의 KG를 통합:

```
[Component Health Summary]
  Tomcat01: trace_anomaly=HIGH, metric_anomaly=CPU_HIGH, log_anomaly=ERROR_BURST
  Tomcat02: trace_anomaly=NONE, metric_anomaly=NONE, log_anomaly=NONE
  ...

[Root Cause Reasoning]
  Trace: Tomcat01 inward spike → Tomcat01 자체 문제
  Metric: Tomcat01 CPU 95% (peer avg 23%)
  Log: Tomcat01에서 OOM 에러 3건
  → Root cause: Tomcat01, high CPU
```

## 8. Implementation Plan

### Phase 1: Trace KG (이번 iteration)

```
파일 구조:
  aiopslab/orchestrator/static_actions/kg/
    __init__.py
    trace_kg_builder.py    — Trace KG 구축 (Strategy A 우선)
    serializer.py          — 텍스트 직렬화 (Format 1 우선)
  clients/
    run_kg_rca.py          — KG + RAG RCA runner

구현 순서:
  1. trace_kg_builder.py: trace 로드 → topology 추출 → anomaly detection → KG 구축
  2. serializer.py: KG → 텍스트 변환 (4가지 포맷)
  3. run_kg_rca.py: KG 구축 → 직렬화 → agent prompt에 주입 → RCA 실행
  4. 테스트: Bank auth-cpu-1 케이스로 검증

검증 기준:
  - KG가 정확한 anomalous edge를 top-5에 포함하는가?
  - LLM이 KG를 읽고 올바른 root cause를 추론하는가?
  - Trace KG만으로 어느 정도 accuracy 달성 가능한가?
```

### Phase 2: Metric KG (다음 iteration)
### Phase 3: Log KG (그다음 iteration)
### Phase 4: Fusion + Optimization

## 9. Open Questions

1. **Top-K 값**: 5가 적절한가? 3, 10과 비교 필요
2. **직렬화 포맷 비교**: 어떤 포맷이 LLM 추론에 가장 효과적인지 A/B 테스트 필요
3. **Unsupervised anomaly detection 정확도**: inject_time 없이 이상 구간을 얼마나 정확히 탐지하는지 검증 필요
4. **KG 크기 vs 프롬프트 길이**: KG가 너무 크면 프롬프트 토큰 한도 초과. top-K로 제어하되 최적 값 탐색 필요
5. **Bank 전용 vs 범용 설계**: Telecom에서 trace KG 효용이 낮은데, 범용으로 갈지 dataset 특화로 갈지
