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

## 6. Orchestrator 구조 (상속 기반)

### 설계 방식: Method C (상속)

공통 로직을 `BaseOrchestrator`로 분리하고, K8s/정적 데이터셋별로 상속.

### 클래스 구조
```python
# aiopslab/orchestrator/base_orchestrator.py (신규)
class BaseOrchestrator:
    """공통 로직: Agent 통신, Session 관리"""

    def __init__(self, results_dir=None):
        self.agent = None
        self.session = None
        self.parser = ResponseParser()

    def register_agent(self, agent, name="agent"):
        self.agent = agent
        self.agent_name = name

    async def ask_agent(self, input):
        """Agent에게 다음 행동 요청"""
        agent_response = await self.agent.get_action(input)
        self.session.add({"role": "assistant", "content": agent_response})
        return agent_response

    async def ask_env(self, input):
        """Agent action을 환경에서 실행"""
        resp = self.parser.parse(input)
        api, args, kwargs = resp["api_name"], resp["args"], resp["kwargs"]
        if api == "submit":
            self.session.set_solution(args[0] if len(args) == 1 else args)
        env_response = self.session.problem.perform_action(api, *args, **kwargs)
        self.session.add({"role": "env", "content": env_response})
        return env_response

    def init_problem(self, problem_id: str):
        """서브클래스에서 구현"""
        raise NotImplementedError

    async def start_problem(self, max_steps: int):
        """서브클래스에서 구현"""
        raise NotImplementedError
```

```python
# aiopslab/orchestrator/orchestrator.py (기존 수정)
class Orchestrator(BaseOrchestrator):
    """K8s 기반 실시간 시스템용"""

    def init_problem(self, problem_id: str):
        # OpenEBS, Prometheus 설정
        # app.deploy(), inject_fault(), start_workload()

    async def start_problem(self, max_steps: int):
        # Agent 루프
        # recover_fault(), app.cleanup(), prometheus.teardown()
```

```python
# aiopslab/orchestrator/static_orchestrator.py (신규)
class StaticDatasetOrchestrator(BaseOrchestrator):
    """정적 데이터셋용 (OpenRCA 등)"""

    def init_problem(self, problem_id: str):
        self.session = Session(results_dir=self.results_dir)
        prob = self.probs.get_problem_instance(problem_id)  # CSV 로드
        self.session.set_problem(prob, pid=problem_id)
        return prob.get_task_description(), prob.get_instructions(), prob.get_available_actions()

    async def start_problem(self, max_steps: int):
        # Agent 루프 (cleanup 없음)
        for step in range(max_steps):
            action = await self.ask_agent(input)
            env_response = await self.ask_env(action)
            if env_response == SubmissionStatus.VALID_SUBMISSION:
                break
        results = self.session.problem.eval(...)
        self.session.set_results(results)
        self.session.to_json()
        return {"results": results, ...}
```

### 파일 구조
```
aiopslab/orchestrator/
├── base_orchestrator.py      # 신규: 공통 로직
├── orchestrator.py           # 수정: BaseOrchestrator 상속
└── static_orchestrator.py    # 신규: 정적 데이터셋용
```

### 장점
- 코드 중복 없음 (ask_agent, ask_env, session 공유)
- 기존 Orchestrator 로직 거의 유지
- 분기문 없이 깔끔한 구조

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

### 9.1 Agent 수정 (react.py 기준)

**1) templates.py에 OpenRCA 템플릿 추가:**
```python
OPENRCA_DOCS = """{prob_desc}
...
## POSSIBLE ROOT CAUSE COMPONENTS:
{candidate_components}

## POSSIBLE ROOT CAUSE REASONS:
{candidate_reasons}
"""
```

**2) init_context에 candidates 파라미터 추가:**
```python
def init_context(self, problem_desc, instructions, apis, candidates=None):
    if candidates:
        self.system_message = OPENRCA_DOCS.format(...,
            candidate_components=candidates["components"],
            candidate_reasons=candidates["reasons"])
    else:
        # 기존 AIOpsLab 로직
```

### 9.2 Submit 형식 차이

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

### 9.3 Instructions 템플릿

```python
OPENRCA_INSTRUCTIONS = """
Analyze the telemetry data using the provided APIs and identify the root cause.

Submit format:
```
submit({
    "root cause occurrence datetime": "YYYY-MM-DD HH:MM:SS",
    "root cause component": "<component_name>",
    "root cause reason": "<reason>"
})
```

IMPORTANT: All API calls must be written inside a markdown code block.
"""
```

### 9.4 Session 결과 확장

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

### 9.5 파일 생성 목록 (최종)

```
aiopslab/
├── orchestrator/
│   ├── base_orchestrator.py         # 신규: 공통 로직 (ask_agent, ask_env, session)
│   ├── orchestrator.py              # 수정: BaseOrchestrator 상속
│   ├── static_orchestrator.py       # 신규: 정적 데이터셋용 Orchestrator
│   ├── problems/
│   │   ├── openrca_registry.py      # 신규: OpenRCA Problem Registry
│   │   └── openrca/
│   │       └── bank_rca.py          # 신규: BankRCAProblem
│   └── actions/
│       └── openrca_bank.py          # 신규: BankActions
├── observer/
│   └── openrca_bank.py              # 신규: BankDataset
└── service/
    └── openrca_dataset/             # 기존: CSV 데이터

clients/
├── utils/
│   └── openrca_templates.py         # 신규: OpenRCA 프롬프트 템플릿
└── run_openrca.py                   # 신규: OpenRCA 실행 스크립트
```

### 9.6 Entry Point / CLI

**실행 스크립트:** `clients/run_openrca.py`
```python
import argparse
import asyncio
from aiopslab.orchestrator.static_orchestrator import StaticDatasetOrchestrator
from clients.react import Agent

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", choices=["bank", "market", "telecom"], required=True)
    parser.add_argument("--task", default=None, help="task_1 ~ task_7")
    parser.add_argument("--query_id", type=int, default=None, help="특정 query만 실행")
    parser.add_argument("--max_steps", type=int, default=30)
    args = parser.parse_args()

    orchestrator = StaticDatasetOrchestrator()
    agent = Agent()
    orchestrator.register_agent(agent, name="react")

    if args.query_id is not None:
        # 단일 쿼리 실행
        problem_id = f"openrca-{args.domain}-{args.task}-{args.query_id}"
        run_single(orchestrator, agent, problem_id, args.max_steps)
    else:
        # 전체 쿼리 배치 실행
        run_batch(orchestrator, agent, args.domain, args.task, args.max_steps)

def run_single(orchestrator, agent, problem_id, max_steps):
    task_desc, instructions, apis = orchestrator.init_problem(problem_id)
    agent.init_context(task_desc, instructions, apis)
    result = asyncio.run(orchestrator.start_problem(max_steps))
    print(f"Result: {result['results']}")

def run_batch(orchestrator, agent, domain, task_filter, max_steps):
    # query.csv에서 문제 목록 로드
    results = []
    for problem_id in get_problem_ids(domain, task_filter):
        result = run_single(orchestrator, agent, problem_id, max_steps)
        results.append(result)
    # 결과 집계
    print(f"Average score: {sum(r['score'] for r in results) / len(results)}")
```

**실행 예시:**
```bash
# 단일 쿼리 실행
python clients/run_openrca.py --domain bank --task task_3 --query_id 5

# Bank 도메인 전체 실행
python clients/run_openrca.py --domain bank

# 특정 태스크 타입만 실행
python clients/run_openrca.py --domain bank --task task_7
```

### 9.7 로깅

**로깅 위치:** `StaticDatasetOrchestrator.start_problem()`

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(f"openrca_{problem_id}.log"),
        logging.StreamHandler()
    ]
)

async def start_problem(self, max_steps: int):
    for step in range(max_steps):
        logging.info(f"Step {step + 1}/{max_steps}")

        action = await self.ask_agent(input)
        logging.info(f"Agent action: {action[:100]}...")

        env_response = await self.ask_env(action)
        logging.info(f"Env response: {str(env_response)[:100]}...")

    logging.info(f"Final results: {results}")
```

**로그 출력 예시:**
```
2024-01-15 10:30:01 [INFO] Step 1/30
2024-01-15 10:30:02 [INFO] Agent action: get_metric_container(start_time="2021-03-04 14:30")...
2024-01-15 10:30:02 [INFO] Env response: timestamp,cmdb_id,kpi_name,value...
2024-01-15 10:30:15 [INFO] Step 5/30
2024-01-15 10:30:16 [INFO] Agent action: submit({"root cause component": "Redis02"...
2024-01-15 10:30:16 [INFO] Final results: {"score": 0.667, "component_match": true...}
```

### 9.8 구현 우선순위

| 순서 | 항목 | 설명 | 상태 |
|------|------|------|------|
| 1 | BaseOrchestrator | 공통 로직 분리 | ✅ 완료 |
| 2 | StaticDatasetOrchestrator | 정적 데이터셋용 Orchestrator | ✅ 완료 |
| 3 | BankDataset | CSV 로드 및 시간 필터링 | ✅ 완료 |
| 4 | BankActions | Agent API (@action 데코레이터) | ✅ 완료 |
| 5 | BankRCAProblem | Problem 클래스 + eval 로직 | ✅ 완료 |
| 6 | OpenRCAProblemRegistry | query.csv → Problem 매핑 | ✅ 완료 |
| 7 | openrca_templates.py | Agent 프롬프트 템플릿 | ✅ 완료 |
| 8 | run_openrca.py | CLI Entry Point | ✅ 완료 |
| 9 | Agent 수정 | init_context 확장 (선택) | ⏳ 미구현 |

---

## 10. 구현 완료 (2025-02-05)

### 10.1 생성된 파일 목록

```
aiopslab/
├── orchestrator/
│   ├── base_orchestrator.py         ✅ 신규
│   ├── orchestrator.py              ✅ 수정 (BaseOrchestrator 상속)
│   ├── static_orchestrator.py       ✅ 신규
│   ├── problems/
│   │   ├── openrca_registry.py      ✅ 신규
│   │   └── openrca/
│   │       ├── __init__.py          ✅ 신규
│   │       └── bank_rca.py          ✅ 신규
│   └── actions/
│       └── openrca_bank.py          ✅ 신규
├── observer/
│   └── openrca_bank.py              ✅ 신규
├── paths.py                         ✅ 수정 (OPENRCA_DATASET_DIR 추가)
└── service/
    └── openrca_dataset/             (기존)

clients/
├── utils/
│   └── openrca_templates.py         ✅ 신규
└── run_openrca.py                   ✅ 신규
```

### 10.2 추가 구현된 기능

| 구현 항목 | 설명 |
|----------|------|
| 밀리초 timestamp 변환 | BankDataset에서 자동 처리 |
| 100행 출력 제한 | BankActions에서 대용량 CSV 대응 |
| scoring_points 자동 파싱 | BankRCAProblem._parse_scoring_points() |
| instruction 날짜 추출 | OpenRCAProblemRegistry._extract_date_from_instruction() |
| CLI 확장 옵션 | --list, --output, --limit, --verbose |

### 10.3 미구현 항목

| 항목 | 설명 |
|------|------|
| Market/Telecom 도메인 | Problem 클래스 미구현 (NotImplementedError) |
| react.py candidates | init_context 파라미터 확장 미적용 |
| 통합 테스트 | 실제 LLM 연동 테스트 필요 |
