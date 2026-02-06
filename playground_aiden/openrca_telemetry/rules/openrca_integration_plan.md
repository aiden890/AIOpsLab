# OpenRCA Dataset → AIOpsLab 통합 계획

## 1. 데이터셋 구조 분석

### OpenRCA 디렉토리 구조
```
openrca_dataset/
├── Bank/
│   ├── record.csv          # 장애 기록 (Ground Truth)
│   ├── query.csv           # 평가 태스크
│   └── telemetry/YYYY_MM_DD/
│       ├── log/log_service.csv
│       ├── metric/metric_app.csv, metric_container.csv
│       └── trace/trace_span.csv
├── Market/cloudbed-{1,2}/  # 동일 구조
└── Telecom/                # 동일 구조
```

### OpenRCA 스키마 (도메인별 상이)

**Metric:**
| 도메인 | metric_container | 서비스 메트릭 |
|--------|------------------|---------------|
| Bank | timestamp, cmdb_id, kpi_name, value | metric_app: timestamp, rr, sr, cnt, mrt, tc |
| Market | 동일 | metric_service: service, timestamp, rr, sr, mrt, count |
| Telecom | itemid, name, bomc_id, timestamp, value, cmdb_id | metric_app: serviceName, startTime, avg_time, num, succee_num, succee_rate |

**Trace:**
| 도메인 | 컬럼 |
|--------|------|
| Bank | timestamp, cmdb_id, parent_id, span_id, trace_id, duration |
| Market | + type, status_code, operation_name, parent_span |
| Telecom | callType, startTime, elapsedTime, success, traceId, id, pid, cmdb_id, dsName, serviceName |

**Log:** Bank/Market 동일 (log_id, timestamp, cmdb_id, log_name, value), Telecom 없음

**Record:** 컬럼 동일, 순서만 다름 (level, component, timestamp, datetime, reason)

---

## 2. 도메인별 API 설계

**공통 파라미터:** `start_time`, `end_time` (Unix timestamp 또는 datetime 문자열)

```python
# Bank
class BankDataset:
    def __init__(self, date: str = None)
    def get_metric_container(self, start_time=None, end_time=None) -> DataFrame
    def get_metric_app(self, start_time=None, end_time=None) -> DataFrame
    def get_traces(self, start_time=None, end_time=None) -> DataFrame
    def get_logs(self, start_time=None, end_time=None) -> DataFrame
    def get_records(self) -> DataFrame
    def get_queries(self) -> DataFrame

# Market
class MarketDataset:
    def __init__(self, cloudbed: str, date: str = None)
    def get_metric_container(self, start_time=None, end_time=None) -> DataFrame
    def get_metric_service(self, start_time=None, end_time=None) -> DataFrame
    def get_metric_mesh(self, start_time=None, end_time=None) -> DataFrame
    def get_metric_node(self, start_time=None, end_time=None) -> DataFrame
    def get_traces(self, start_time=None, end_time=None) -> DataFrame
    def get_logs(self, start_time=None, end_time=None) -> DataFrame
    def get_records(self) -> DataFrame
    def get_queries(self) -> DataFrame

# Telecom
class TelecomDataset:
    def __init__(self, date: str = None)
    def get_metric_container(self, start_time=None, end_time=None) -> DataFrame
    def get_metric_app(self, start_time=None, end_time=None) -> DataFrame
    def get_traces(self, start_time=None, end_time=None) -> DataFrame
    def get_records(self) -> DataFrame
    def get_queries(self) -> DataFrame
```

**시간 필터링:** `None`이면 전체, 지정 시 해당 범위만 반환

---

## 3. 평가 태스크 타입

OpenRCA query.csv의 `task_index` 분류:

| Task | 목표 |
|------|------|
| task_1 | Root Cause **발생 시간** 예측 |
| task_2 | Root Cause **원인** 예측 |
| task_3 | Root Cause **컴포넌트** 예측 |
| task_4 | 발생 시간 + 원인 예측 |
| task_5 | 발생 시간 + 컴포넌트 예측 |
| task_6 | 컴포넌트 + 원인 예측 |
| task_7 | 발생 시간 + 컴포넌트 + 원인 (전체) |

---

## 4. Agent Actions 설계

기존 `TaskActions` 상속 시 Prometheus/Jaeger 기반 API가 포함되어 invalid.
→ **도메인별 독립 Actions 클래스** 생성 (상속 없음)

```python
# openrca_bank.py
class BankActions:  # TaskActions 상속 안함
    def __init__(self, dataset: BankDataset):
        self.dataset = dataset

    @read
    def get_metric_container(self, start_time: str = None, end_time: str = None) -> str:
        """Bank container 메트릭 조회 (시간 범위 지정 가능)"""

    @read
    def get_metric_app(self, start_time: str = None, end_time: str = None) -> str:
        """Bank app 메트릭 조회"""

    @read
    def get_traces(self, start_time: str = None, end_time: str = None) -> str:
        """Bank 트레이스 조회"""

    @read
    def get_logs(self, start_time: str = None, end_time: str = None) -> str:
        """Bank 로그 조회"""

    @action
    def submit(self, component: str, reason: str, timestamp: str):
        """RCA 결과 제출"""
```

Agent API 수집: `get_actions("openrca_bank")` → Bank 전용 API만 반환

---

## 5. 실행 흐름 비교

### 기존 AIOpsLab (실시간 시스템)
```
init_problem() → app.deploy() → inject_fault() → start_workload()
     ↓
Agent Loop: Prometheus/Jaeger/kubectl 실시간 쿼리 → submit()
     ↓
eval() → recover_fault() → app.delete()
```

### OpenRCA (정적 데이터셋)
```
init_problem() → dataset.load() (CSV 로드, 인프라 불필요)
     ↓
Agent Loop: CSV 시간 필터링 → submit(component, reason, timestamp)
     ↓
eval() → record.csv 비교 (cleanup 없음)
```

### 핵심 차이
| 항목 | AIOpsLab | OpenRCA |
|------|----------|---------|
| 인프라 | K8s 클러스터 필요 | 파일만 필요 |
| 데이터 | 실시간 갱신 | 정적 스냅샷 |
| 지원 태스크 | Detection, RCA, Mitigation | **RCA만** |
| Agent 로직 | 동일 (API 호출 → 분석 → 진단) | 동일 |

---

## 6. 실행 스크립트 구조

### 기존 AIOpsLab

**Entry Points:** `service.py` (FastAPI), `assessment.py` (CLI)

**핵심:** `aiopslab/orchestrator/orchestrator.py`
```python
class Orchestrator:
    def init_problem(self, problem_id):
        # 인프라 설정
        self.prometheus.deploy()
        prob.app.deploy()
        prob.inject_fault()
        prob.start_workload()  # wrk2로 트래픽 생성
        return task_desc, instructions, actions

    async def start_problem(self, max_steps):
        for step in range(max_steps):
            action = await self.ask_agent(input)
            env_response = await self.ask_env(action)
        results = prob.eval()
        prob.recover_fault()
        prob.app.cleanup()
```

### OpenRCA (신규 생성)

**신규 파일:** `aiopslab/orchestrator/openrca_orchestrator.py`
```python
class OpenRCAOrchestrator:
    def init_problem(self, domain: str, problem_id: int):
        # CSV 파일 → DataFrame 로드 (인프라 불필요)
        self.dataset = BankDataset(date="2021_03_04")
        self.actions = BankActions(self.dataset)

        query = self.dataset.get_queries().iloc[problem_id]
        return task_desc, instructions, actions

    async def start_problem(self, max_steps):
        for step in range(max_steps):
            action = await self.ask_agent(input)
            env_response = await self.ask_env(action)
        # record.csv와 비교
        results = self.eval(solution)
```

### 파일 구조
```
aiopslab/orchestrator/
├── orchestrator.py           # 기존 (K8s 기반)
└── openrca_orchestrator.py   # 신규 (CSV 기반)
```

---

## 7. Problem 등록 구조

### 기존 AIOpsLab

**Registry:** `aiopslab/orchestrator/problems/registry.py`
```python
class ProblemRegistry:
    PROBLEM_REGISTRY = {
        # problem_id → Problem 클래스 (하드코딩)
        "misconfig_app_hotel_res-detection-1": MisconfigAppDetection,
        "misconfig_app_hotel_res-mitigation-1": MisconfigAppMitigation,
    }
```

**Problem 클래스:** `aiopslab/orchestrator/problems/{name}/{name}.py`
```python
class MisconfigAppBaseTask:
    def __init__(self):
        self.app = HotelReservation()
        self.faulty_service = "geo"

    def inject_fault(self): ...   # Chaos Mesh
    def recover_fault(self): ...
    def start_workload(self): ... # wrk2

class MisconfigAppDetection(BaseTask, DetectionTask):
    def eval(self, soln, trace, duration): ...
```

**Problem ID 형식:** `{문제명}-{태스크타입}-{인스턴스번호}`

### OpenRCA (신규 생성)

**Registry:** `aiopslab/orchestrator/problems/openrca_registry.py`
```python
class OpenRCAProblemRegistry:
    @classmethod
    def from_query_csv(cls, domain: str):
        """query.csv에서 문제 목록 자동 생성"""
        queries = pd.read_csv(f"openrca_dataset/{domain}/query.csv")
        registry = {}
        for idx, row in queries.iterrows():
            pid = f"{domain}-{row['task_index']}-{idx}"
            registry[pid] = lambda i=idx: BankRCAProblem(row_id=i)
        return registry
```

**Problem 클래스:** `aiopslab/orchestrator/problems/openrca_bank.py`
```python
class BankRCAProblem:
    def __init__(self, row_id: int):
        self.dataset = BankDataset()
        self.actions = BankActions(self.dataset)
        query = self.dataset.get_queries().iloc[row_id]
        self.instruction = query["instruction"]        # Agent에게 제공
        self.scoring_points = query["scoring_points"]  # 평가 기준

    def get_task_description(self):
        return self.instruction  # "March 4, 14:30~15:00에 장애 발생..."

    def inject_fault(self): pass   # 불필요
    def recover_fault(self): pass
    def start_workload(self): pass

    def eval(self, soln, trace, duration):
        """scoring_points 기준으로 평가"""
        return {"component_match": ..., "reason_match": ...}
```

**Agent 동작:** instruction에서 시간 범위 파악 → API 호출 시 직접 지정
```
instruction: "March 4, 2021, 14:30~15:00에 장애 발생..."
     ↓
Agent: get_metric_container(start_time="2021-03-04 14:30", end_time="2021-03-04 15:00")
```

**Problem ID 형식:** `{도메인}-{task_index}-{row_id}`

### 비교
| 항목 | AIOpsLab | OpenRCA |
|------|----------|---------|
| 문제 소스 | 코드에 하드코딩 | query.csv 자동 생성 |
| inject_fault | Chaos Mesh 실행 | 불필요 (pass) |
| eval | 시스템 상태 확인 | scoring_points 비교 |

---

## 8. Evaluation 구조

### OpenRCA 평가 방식

**후보 목록 (Closed-set):** Agent는 미리 정의된 목록에서 선택
```
Components: apache01, apache02, Tomcat01~04, MG01~02, IG01~02, Mysql01~02, Redis01~02
Reasons: high CPU usage, high memory usage, network latency, network packet loss, ...
```

**평가 기준:**
| 항목 | 비교 방식 |
|------|----------|
| Component | 정확히 일치 (`==`) |
| Reason | 정확히 일치 (`==`) |
| Time | ±1분(60초) 이내 |

**점수 계산:** `score = 맞춘 항목 수 / 전체 항목 수`

### AIOpsLab 구현

**Task별 평가 필드 매핑:**
```python
TASK_EVAL_FIELDS = {
    "task_1": ["time"],
    "task_2": ["reason"],
    "task_3": ["component"],
    "task_4": ["time", "reason"],
    "task_5": ["time", "component"],
    "task_6": ["component", "reason"],
    "task_7": ["time", "component", "reason"],
}
```

**Problem 클래스:**
```python
class BankRCAProblem:
    CANDIDATE_COMPONENTS = ["apache01", "apache02", "Tomcat01", ...]
    CANDIDATE_REASONS = ["high CPU usage", "high memory usage", ...]

    def get_task_description(self):
        return f"{self.instruction}\n\nComponents: {self.CANDIDATE_COMPONENTS}\nReasons: {self.CANDIDATE_REASONS}"

    def eval(self, soln, trace, duration):
        gt = self._parse_scoring_points()
        fields = TASK_EVAL_FIELDS[self.task_index]
        score, total = 0, len(fields)

        if "component" in fields:
            score += (soln["component"] == gt["component"])
        if "reason" in fields:
            score += (soln["reason"] == gt["reason"])
        if "time" in fields:
            score += self._time_within_1min(soln["timestamp"], gt["timestamp"])

        return {"score": score / total, ...}

    def _time_within_1min(self, pred, gt):
        return abs((parse(pred) - parse(gt)).total_seconds()) <= 60
```

### 기존 AIOpsLab vs OpenRCA eval 비교
| 항목 | AIOpsLab | OpenRCA |
|------|----------|---------|
| 평가 기준 | 시스템 상태 (pod ready) | scoring_points 파싱 |
| 점수 형식 | boolean (success/fail) | 비율 (0.0 ~ 1.0) |
| Time 평가 | 해당 없음 | ±1분 허용 |

---

## 9. 추가 구현 필요 사항

### 9.1 Agent Prompt 구성

**기존 AIOpsLab:**
```python
# clients/utils/templates.py
DOCS_SHELL_ONLY = """{prob_desc}
You are provided with a direct API to a secure terminal:
{shell_api}
Finally, submit your solution:
{submit_api}
"""
```

**OpenRCA 신규 템플릿 필요:**
```python
# clients/utils/openrca_templates.py
OPENRCA_PROMPT = """{prob_desc}

## Available APIs:
{telemetry_apis}

## Submit API:
{submit_api}

## POSSIBLE ROOT CAUSE COMPONENTS:
{candidate_components}

## POSSIBLE ROOT CAUSE REASONS:
{candidate_reasons}

Note: Component와 Reason은 반드시 위 후보 목록에서 선택해야 합니다.
"""
```

### 9.2 Agent init_context 수정

**기존:** shell_api 중심
**OpenRCA:** telemetry API 중심 + 후보 목록 포함

```python
# OpenRCA Agent 또는 기존 Agent 확장
def init_context(self, problem_desc, instructions, apis, candidates=None):
    # OpenRCA인 경우 후보 목록 추가
    if candidates:
        self.system_message = OPENRCA_PROMPT.format(
            prob_desc=problem_desc,
            telemetry_apis=stringify_apis(apis),
            submit_api=...,
            candidate_components=candidates["components"],
            candidate_reasons=candidates["reasons"],
        )
```

### 9.3 Submit 형식 차이

**기존 AIOpsLab:**
```python
submit("Yes")  # Detection
submit({"system_level": "...", "fault_type": "..."})  # Analysis
```

**OpenRCA:**
```python
submit({
    "root cause occurrence datetime": "2021-03-04 14:57:00",
    "root cause component": "Redis02",
    "root cause reason": "high memory usage"
})
```

### 9.4 Instructions 템플릿

```python
# OpenRCA용 instructions
OPENRCA_INSTRUCTIONS = """
텔레메트리 API를 사용하여 데이터를 분석하고 Root Cause를 찾으세요.

응답 형식:
```
submit({
    "root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS",
    "root cause component": "<component_name>",
    "root cause reason": "<reason>"
})
```

주의: 모든 API 호출은 markdown 코드 블록 안에 작성하세요.
"""
```

### 9.5 Session 결과 확장

**기존 Session 출력:**
```json
{
    "results": {
        "TTM": 150.333,
        "steps": 5,
        "success": true
    }
}
```

**OpenRCA 추가 필드:**
```json
{
    "results": {
        "score": 0.667,
        "component_match": true,
        "reason_match": true,
        "time_match": false,
        "steps": 5,
        "task_index": "task_7"
    }
}
```

### 9.6 파일 생성 목록 (최종)

```
aiopslab/
├── orchestrator/
│   ├── openrca_orchestrator.py      # 신규: OpenRCA용 Orchestrator
│   ├── problems/
│   │   ├── openrca_registry.py      # 신규: OpenRCA Problem Registry
│   │   └── openrca_bank.py          # 신규: BankRCAProblem
│   └── actions/
│       └── openrca_bank.py          # 신규: BankActions
├── observer/
│   └── openrca_bank.py              # 신규: BankDataset
└── service/
    └── openrca_dataset/             # 기존: CSV 데이터

clients/
└── utils/
    └── openrca_templates.py         # 신규: OpenRCA 프롬프트 템플릿
```

### 9.7 구현 우선순위

| 순서 | 항목 | 설명 |
|------|------|------|
| 1 | BankDataset | CSV 로드 및 시간 필터링 |
| 2 | BankActions | Agent API (@action 데코레이터) |
| 3 | BankRCAProblem | Problem 클래스 + eval 로직 |
| 4 | OpenRCAProblemRegistry | query.csv → Problem 매핑 |
| 5 | OpenRCAOrchestrator | 실행 흐름 (init/start/eval) |
| 6 | openrca_templates.py | Agent 프롬프트 템플릿 |
| 7 | Agent 수정 | init_context 확장 (선택) |
