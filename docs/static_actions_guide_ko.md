# Static App 액션 가이드

> OpenRCA 정적 데이터셋 RCA(근본 원인 분석)에서 사용할 수 있는 모든 액션을 텔레메트리 유형별로 설명합니다.

---

## 목차

1. [메트릭(Metric) 액션](#1-메트릭metric-액션)
2. [트레이스(Trace) 액션](#2-트레이스trace-액션)
3. [실행기(Executor) 액션](#3-실행기executor-액션)
4. [가설(Hypothesis) 액션](#4-가설hypothesis-액션)
5. [제출(Submit) 액션](#5-제출submit-액션)
6. [권장 분석 순서](#6-권장-분석-순서)

---

## 1. 메트릭(Metric) 액션

메트릭 액션은 KPI(핵심 성과 지표)의 이상 편차를 분석하여 장애 구성 요소를 식별합니다.
모든 KPI 액션은 **가능한 루트 원인 컴포넌트 목록으로 자동 필터링**됩니다.

---

### `get_kpi_high_deviation(namespace, start_time, end_time)`

**목적**: 장애 시간 윈도우 내에서 전역 P90 기준선을 초과한 KPI를 가진 컴포넌트를 반환합니다.

**언제 사용하나요?**
- CPU 과부하, 네트워크 지연(latency), JVM 메모리 과다 사용 등 **값이 급증**하는 장애를 탐지할 때

**반환 컬럼 설명**

| 컬럼 | 설명 |
|---|---|
| `component` | 컴포넌트 이름 (예: Tomcat01) |
| `kpi` | KPI 이름 (예: CPUCpuUtil, NETKBTotalPerSec) |
| `p90` | 해당 KPI의 전역 P90 기준값 |
| `max_value` | 시간 윈도우 내 최댓값 |
| `deviation_above_p90` | 기준값 초과량 |
| `peak_high_ts` | 최댓값 발생 정확한 타임스탬프 → **가설 저장 시 사용** |

**KPI별 장애 유형 매핑**

| KPI 패턴 | 의미하는 장애 유형 |
|---|---|
| `NETKBTotalPerSec`, `NETPackets*` 급증 | `"network latency"` (네트워크 지연) |
| `CPUCpuUtil` 급증 | `"high CPU usage"` (CPU 과부하) |
| `used_memory`, `JVMUsedMemory`, `HeapMemoryUsed` 급증 | `"high memory usage"` (메모리 과다 사용) |
| `DSKRead`, 디스크 관련 KPI 급증 | `"high disk I/O read usage"` |
| `JVMCpuLoad` 급증 | `"high JVM CPU load"` |

**주의사항**
- JVM 메모리 행이 목록 상단을 차지해도 **NET* 행을 반드시 확인**하세요 — 작은 편차라도 네트워크 장애의 직접적인 신호입니다.
- 여러 컴포넌트에서 NET* 급증이 보이면 `get_component_kpi_deviation()`으로 각각 확인하세요.

---

### `get_kpi_low_deviation(namespace, start_time, end_time)`

**목적**: 장애 시간 윈도우 내에서 전역 P10 기준선 아래로 떨어진 KPI를 가진 컴포넌트를 반환합니다.

**언제 사용하나요?**
- 네트워크 패킷 손실, DB 연결 제한 등 **값이 급락**하는 장애를 탐지할 때

**반환 컬럼 설명**

| 컬럼 | 설명 |
|---|---|
| `component` | 컴포넌트 이름 |
| `kpi` | KPI 이름 |
| `p10` | 해당 KPI의 전역 P10 기준값 |
| `min_value` | 시간 윈도우 내 최솟값 |
| `deviation_below_p10` | 기준값 하회량 |
| `peak_low_ts` | 최솟값 발생 정확한 타임스탬프 → **가설 저장 시 사용** |

**KPI별 장애 유형 매핑**

| KPI 패턴 | 의미하는 장애 유형 |
|---|---|
| `NETKBTotalPerSec`, `NETPackets*` 급락 | `"network packet loss"` (네트워크 패킷 손실) |

**중요 규칙**
- NET* KPI가 **P10 이하 급락** → `"network packet loss"`
- NET* KPI가 **P90 이상 급증** (`get_kpi_high_deviation`) → `"network latency"`
- 트레이스만으로 패킷 손실과 지연을 구분하지 마세요 — 반드시 KPI 방향으로 확인하세요.

**여러 컴포넌트가 NET* 급락을 보일 때**
- 각 컴포넌트를 `get_component_kpi_deviation()`으로 개별 확인
- **가장 이른 `peak_low_ts`를 가진 컴포넌트**가 루트 원인 — 장애는 원점에서 피해 컴포넌트로 전파됩니다

---

### `get_kpi_deviation_table(namespace, start_time, end_time)`

**목적**: 가능한 모든 루트 원인 컴포넌트에 대한 **컴포넌트당 1행** 요약 테이블을 반환합니다.

**언제 사용하나요?**
- 분석 초반에 모든 컴포넌트의 이상 상태를 한눈에 파악할 때
- 어떤 컴포넌트를 더 자세히 조사할지 우선순위를 정할 때

**반환 컬럼 설명**

| 컬럼 | 설명 |
|---|---|
| `component` | 컴포넌트 이름 |
| `max_high_dev` | P90 기준선 초과 최댓값 |
| `peak_high_ts` | 최고값 발생 타임스탬프 |
| `max_low_dev` | P10 기준선 하회 최댓값 |
| `peak_low_ts` | 최저값 발생 타임스탬프 |

---

### `get_component_kpi_deviation(namespace, component, start_time, end_time)`

**목적**: 특정 컴포넌트 하나에 대해 모든 KPI의 HIGH(P90 초과) 및 LOW(P10 미만) 편차를 상세히 반환합니다.

**언제 사용하나요?**
- 특정 컴포넌트의 장애 유형과 정확한 발생 시각을 확인할 때
- `get_kpi_high/low_deviation()`에서 해당 컴포넌트가 탐지된 후 세부 확인 시
- **Redis*, Mysql* 등 DB 컴포넌트** — 트레이스에서 신호가 없으므로 반드시 이 액션으로 직접 확인

**활용 예시**
```
get_component_kpi_deviation("static-bank", "Tomcat02", "2021-03-09 11:00:00", "2021-03-09 11:30:00")
```

**DB/캐시 컴포넌트 확인 시 주목할 KPI**
- `used_memory`, `JVMUsedMemory` → `"high memory usage"`
- Redis/Mysql은 콜 체인의 **말단 노드** — 트레이스 callee 신호가 항상 0이므로 KPI로만 탐지 가능

---

## 2. 트레이스(Trace) 액션

트레이스 액션은 분산 추적 데이터를 분석하여 서비스 간 호출 관계와 네트워크 장애를 탐지합니다.

---

### `get_trace_call_graph(namespace, start_time, end_time, faulty_components=None)`

**목적**: 3가지 신호를 결합해 가장 깊은 장애 컴포넌트(루트 원인 후보)를 식별합니다.

**언제 사용하나요?**
- 메트릭에서 여러 컴포넌트가 이상으로 탐지된 후, **어느 컴포넌트가 실제 원점인지** 좁힐 때
- 특히 Tomcat*, IG*, MG* 계층에서 네트워크 장애 방향을 확인할 때

**3가지 신호 설명**

| 신호 | 계산 방법 | 의미 |
|---|---|---|
| **A (Callee)** | `fail_rate × 100` | 해당 컴포넌트가 서버로 호출될 때 실패율 → DB/서비스 장애 |
| **B (Caller)** | `avg_elapsed / peer_median` | 같은 유형 컴포넌트 대비 경과 시간 비율 → CPU/네트워크 장애 |
| **C (Network Gap)** | `(parent.duration - Σchild.duration) / peer_median` | 네트워크 전송 시간 비율 → 네트워크 지연/패킷 손실 |
| **Combined** | `A×3 + B + C×2` | 종합 점수 |

**Signal 유형 해석**

| signal | 의미 |
|---|---|
| `callee` | A > 1.0 → 서버로 호출 시 실패 → DB/서비스 직접 장애 |
| `network_gap` | C > B → 네트워크 시간 소비 → **KPI 방향으로 패킷 손실 vs 지연 구분 필수** |
| `caller` | B가 높음 → 클라이언트 역할 시 느림 → 호출 대상의 장애 영향 |

**반환 예시**
```
Trace call graph analysis for 'static-bank':
  Best candidate: Tomcat02
  Signal type:    network_gap — network fault. Resolve type: NET* drop = packet loss; NET* spike = latency
  Trace time range: 2021-03-09 11:00:00 ~ 2021-03-09 11:30:00 UTC

Top candidates (combined = callee×3 + caller + net_gap×2):
  Tomcat02: combined=3.85  (callee=0.00, caller=1.23, net_gap=1.31)
  Tomcat03: combined=2.10  (callee=0.00, caller=0.98, net_gap=0.56)

Network gap stats (parent.duration - sum(child.duration)):
  Tomcat02: gap_ratio=0.421  avg_gap=390ms / avg_parent=926ms  (n=1842)
```

**주의사항**
- `network_gap` 신호만으로는 패킷 손실인지 지연인지 알 수 없음 → `get_component_kpi_deviation()`으로 NET* KPI 방향 확인
- `apache*` 컴포넌트의 패킷 손실은 **apache 자체의 KPI에서 NET* 급락**으로 나타남 — 트레이스에서는 apache를 호출하는 Tomcat/IG 컴포넌트에서 큰 gap이 보일 수 있음 (피해자)
- `faulty_components` 미지정 시 자동으로 가능한 루트 원인 컴포넌트 목록으로 제한됨

---

### `get_hypothesis_causal_graph(namespace, start_time, end_time, components=None)`

**목적**: 저장된 가설 컴포넌트들을 중심으로 콜 체인을 시각화하고, 각 엣지의 평균 경과 시간과 네트워크 갭을 보여줍니다.

**언제 사용하나요?**
- Phase 3에서 가설 간의 인과 관계(어느 컴포넌트가 원점이고 어느 것이 피해자인지)를 파악할 때
- `submit()` 호출 전 필수 단계 (트레이스 활성화 시)

**파라미터**

| 파라미터 | 설명 |
|---|---|
| `namespace` | 네임스페이스 (예: "static-bank") |
| `start_time` | 장애 시간 윈도우 시작 |
| `end_time` | 장애 시간 윈도우 종료 |
| `components` | 저장된 가설 외에 추가로 포함할 컴포넌트 목록 (선택) |

**반환 정보**
- `caller → callee` 콜 체인 (저장된 가설 컴포넌트 레이블 포함)
- `avg_elapsed`: 평균 경과 시간 (ms)
- `net_gap`: 해당 caller 컴포넌트의 네트워크 갭 (ms 및 %)

**활용 팁**
- `components=["apache02", "Redis02"]` 처럼 아직 가설로 저장하지 않은 후보도 포함해 비교 가능
- 자기 참조 엣지(A → A)는 내부 스팬으로 자동 제거됨

---

## 3. 실행기(Executor) 액션

Executor 액션은 자연어 지시를 Python 코드로 변환하여 IPython 커널에서 실행합니다.
변수는 호출 간에 유지되므로 불필요한 재조회를 피할 수 있습니다.

---

### `execute(instruction)`

**목적**: 커스텀 텔레메트리 분석을 위한 Python 코드를 생성하고 실행합니다.

**언제 사용하나요?**
- KPI 테이블 출력이 잘려(truncated) 정확한 `peak_high_ts`/`peak_low_ts`를 읽지 못할 때
- 트레이스 데이터에서 `parent_id → span_id` 조인으로 네트워크 갭을 직접 계산할 때
- 여러 컴포넌트의 타임스탬프를 비교해 가장 이른 장애 발생 시각을 찾을 때

**활용 예시**
```
execute("metrics.csv에서 Tomcat01과 Tomcat02의 NETKBTotalPerSec KPI를 읽고,
         P10 이하로 떨어지는 정확한 타임스탬프를 각각 추출해 비교하세요")
```

**주의사항**
- 데이터 조회 및 계산 전용 — 결론 도출이나 루트 원인 판단은 컨트롤러(에이전트)의 역할
- 시각화(matplotlib 등) 불가
- 로컬 파일 저장 불가

---

### `analyze_hypothesis_relationships(instruction)`

**목적**: 저장된 모든 가설을 컨텍스트로 제공하고 Executor LLM으로 인과 관계 분석을 수행합니다.

**언제 사용하나요?**
- Phase 3에서 `submit()` 전 필수 — 어느 컴포넌트가 실제 원점이고 어느 것이 피해자/배경 장애인지 분석할 때
- Executor와 Hypothesis 모두 활성화된 경우에만 사용 가능

**내부 동작**
1. 저장된 모든 가설(컴포넌트, 이유, 타임스탬프, 근거)을 자동으로 컨텍스트에 포함
2. 지시(instruction)와 함께 Executor에게 분석 요청

**권장 instruction 패턴**
```
"각 가설 쌍에 대해 하나의 장애가 다른 것을 유발했는지 판단하세요.
 전체 전파 체인을 작성하고 아래 규칙으로 루트 원인을 식별하세요:
 - 독립성 ≠ 루트 원인: 다른 가설로 설명되지 않는다고 해서 루트 원인이 아닙니다
 - 서비스 콜 경로에서 가장 큰 경과 시간/네트워크 갭을 가진 컴포넌트 우선
 - DB(Redis/Mysql) 메모리 압박은 배경 장애일 수 있음"
```

---

## 4. 가설(Hypothesis) 액션

가설 액션은 분석 중 발견한 루트 원인 후보를 체계적으로 추적합니다.

---

### `save_hypothesis(component, reason, datetime, confidence, evidence)`

**목적**: 루트 원인 후보 가설을 저장합니다.

**파라미터**

| 파라미터 | 설명 | 예시 |
|---|---|---|
| `component` | 루트 원인 후보 컴포넌트 (가능한 목록에서 선택) | `"Tomcat02"` |
| `reason` | 장애 유형 (가능한 이유 목록에서 선택) | `"network packet loss"` |
| `datetime` | 장애 발생 정확한 타임스탬프 (텔레메트리에서 읽은 값) | `"2021-03-09 11:14:00"` |
| `confidence` | 확신도: `"high"`, `"medium"`, `"low"` | `"high"` |
| `evidence` | 텔레메트리 출처와 타임스탬프를 포함한 근거 문장 | `"metric: Tomcat02 NETKBTotalPerSec dropped below P10 at 2021-03-09 11:14:00"` |

**datetime 거부 조건**

| 거부 조건 | 메시지 |
|---|---|
| `HH:00:00` 또는 `HH:30:00` 형태 (추정값) | "round estimate — 텔레메트리의 정확한 값 사용" |
| 쿼리 시간 범위 밖 | "outside query time range" |
| 가능한 컴포넌트 목록에 없는 component | "not a valid root cause component" |
| 모호한 reason | "too vague — possible reasons list에서 선택" |

**제출 전 최소 3개의 가설 필요**

---

### `get_hypotheses()`

**목적**: 저장된 모든 가설을 확신도 순으로 정렬해 반환합니다.

**언제 사용하나요?**
- Phase 3에서 모든 후보를 검토하기 전
- 어떤 컴포넌트를 아직 조사하지 않았는지 확인할 때

---

## 5. 제출(Submit) 액션

### `submit(prediction)`

**목적**: 루트 원인 분석 결과를 제출합니다.

**형식**
```python
submit({
    "1": {
        "root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS",
        "root cause component": "component_name",
        "root cause reason": "fault_reason"
    }
})
```

**다중 장애 시 (N개 실패가 보고된 경우)**
```python
submit({
    "1": {"root cause component": "Tomcat02", "root cause reason": "network latency", ...},
    "2": {"root cause component": "MG01", "root cause reason": "network packet loss", ...}
})
```

**제출 전 게이트 (Hypothesis 모드)**

| 순서 | 조건 | 실패 시 메시지 |
|---|---|---|
| 1 | 최소 3개 가설 저장 | "only N hypothesis saved (M more needed)" |
| 2 | `get_hypothesis_causal_graph()` 호출 (트레이스 활성화 시) | "must call get_hypothesis_causal_graph() before submitting" |
| 3 | `analyze_hypothesis_relationships()` 호출 (Executor 활성화 시) | "must call analyze_hypothesis_relationships() before submitting" |
| 4 | HIGH 확신도 가설 1개 이상 존재 | "none of the N saved hypotheses has HIGH confidence" |

---

## 6. 권장 분석 순서

```
Phase 1 — 컴포넌트 레벨별 후보 탐색
│
├── get_kpi_high_deviation()         # P90 초과 KPI 확인 (CPU, 메모리, NET* 급증)
├── get_kpi_low_deviation()          # P10 미만 KPI 확인 (NET* 급락 = 패킷 손실)
│
├── [NET* 이상이 여러 컴포넌트에서 발생한 경우]
│   └── get_component_kpi_deviation() × N  # 각 컴포넌트 개별 확인, peak_ts 비교
│       → 가장 이른 타임스탬프 = 루트 원인 원점
│
├── get_trace_call_graph()           # 트레이스로 최심 장애 노드 확인
│   └── network_gap 신호 → get_component_kpi_deviation()으로 패킷 손실 vs 지연 구분
│
└── save_hypothesis() × 3+          # 각 레벨 최우수 후보 저장

Phase 2 — 이유 유형별 후보 탐색
│
└── 가능한 이유 목록의 각 유형에 대해 최적 후보 저장

Phase 3 — 관계 분석 후 제출
│
├── get_hypothesis_causal_graph()    # 가설 컴포넌트 간 콜 체인 시각화 (필수)
├── analyze_hypothesis_relationships() # 원점 vs 피해자 판단 (필수)
└── submit()                         # 최종 루트 원인 제출
```

---

## 참고: KPI 신호 → 장애 이유 매핑 요약표

| KPI 패턴 | 방향 | 사용할 이유 문자열 |
|---|---|---|
| `NETKBTotalPerSec`, `NETPackets*` | **급락** (P10 미만) | `"network packet loss"` |
| `NETKBTotalPerSec`, `NETPackets*` | **급증** (P90 초과) | `"network latency"` |
| `used_memory`, `JVMUsedMemory`, `HeapMemoryUsed` | P90 초과 | `"high memory usage"` |
| `CPUCpuUtil` | P90 초과 | `"high CPU usage"` |
| `DSKRead`, 디스크 KPI | P90 초과 | `"high disk I/O read usage"` |
| 디스크 공간 KPI | P90 초과 | `"high disk space usage"` |
| `JVMCpuLoad` | P90 초과 | `"high JVM CPU load"` |
| 로그에 `java.lang.OutOfMemoryError` 또는 힙 사용률 95%+ | — | `"JVM Out of Memory (OOM) Heap"` |
