# Static Dataset v2 - 최종 결과

**작성일**: 2026-02-10
**버전**: v2 (OpenRCA 통합 리팩토링)

---

## 1. 주요 변경 요약 (v1 → v2)

### 핵심 변경

| 항목 | v1 | v2 |
|------|-----|-----|
| Task 매핑 | task_1→Detection, task_3→Localization 등 분리 | **단일 OpenRCATask** — 모든 task type 통합 |
| Ground truth | "Yes", "Mysql02" 하드코딩 | **scoring_points 기반 동적 평가** (OpenRCA evaluate.py 포팅) |
| 문제 수 | 7개 하드코딩 | **~330개 동적 생성** (query.csv row 수만큼) |
| Submit 형식 | task별 다름 ("Yes"/"No", ["comp1"]) | **통일 JSON**: `{"1": {"root cause occurrence datetime": ..., "root cause component": ..., "root cause reason": ...}}` |
| 평가 로직 | task별 커스텀 | **OpenRCA evaluate.py 그대로** — regex 매칭 + permutation 최적 정렬 |
| Config 경로 | macOS 하드코딩 | **상대 경로** (aiopslab-applications/static_dataset/...) |
| Docker | named volume + 중복 processing | **bind mount** `/tmp/aiopslab-telemetry` + 단순화 entrypoint |
| Telemetry 처리 | entrypoint.sh에서 Python inline | **dataset.py에서 Python native** (TimeRemapper 적용) |

---

## 2. 실행 방법

### 유닛 테스트 실행

```bash
python -m pytest tests/test_static_dataset.py -v
```

### 문제 실행 (전체 파이프라인)

```python
from aiopslab.orchestrator.static_orchestrator import StaticOrchestrator

orch = StaticOrchestrator()

# 문제 목록 조회
all_ids = orch.probs.get_problem_ids()                              # ~330개
bank_ids = orch.probs.get_problem_ids(dataset="openrca_bank")       # ~120개
task7_ids = orch.probs.get_problem_ids(task_type="task_7")          # Hard 문제들

# 특정 문제 실행
orch.init_problem("openrca_bank-task_1-0")
# Agent → get_logs(), get_metrics(), get_traces()
# Agent → submit({"1": {"root cause occurrence datetime": "2021-03-04 14:57:00"}})
```

---

## 3. 테스트 결과

```
38 passed in 1.00s
```

| 테스트 클래스 | 테스트 수 | 결과 |
|---|---|---|
| TestStaticApp | 7 | 7/7 PASSED |
| TestStaticProblemRegistry | 8 | 8/8 PASSED |
| TestOpenRCAEvaluator | 8 | 8/8 PASSED |
| TestBaseOrchestrator | 4 | 4/4 PASSED |
| TestStaticActions | 4 | 4/4 PASSED |
| TestOpenRCATask | 2 | 2/2 PASSED |
| TestOpenRCAProblemClasses | 5 | 5/5 PASSED |

### 테스트 범위

- **StaticApp**: 로그/메트릭/트레이스 읽기, 서비스 필터, 누락 네임스페이스, 저장, 삭제
- **StaticProblemRegistry**: 동적 로드, Bank/Telecom 문제 수, task_type 필터, dataset 필터, ID 형식, 배포 타입
- **OpenRCAEvaluator**: task_1/3/7 정답, 오답, 부분 점수(0.67), 시간 ±1분, 빈 prediction, 난이도
- **BaseOrchestrator**: import, 상속 관계, StaticOrchestrator → StaticProblemRegistry
- **StaticActions**: RCA import, submit action, base 메서드, detection 하위호환
- **OpenRCATask**: import, Task 상속
- **ProblemClasses**: 4개 문제 클래스 import, 다중 상속 확인

---

## 4. 최종 디렉토리 구조

```
aiopslab/
├── service/
│   ├── static_app.py                    ← 서비스 클라이언트 (CSV 읽기/쓰기)
│   ├── metadata/static-dataset.json     ← 메타데이터
│   └── apps/static_dataset/
│       ├── dataset.py                   ← StaticDataset (deploy + telemetry processing)
│       ├── config/
│       │   ├── openrca_bank.json        ← 상대 경로로 수정
│       │   ├── openrca_telecom.json
│       │   ├── openrca_market_cloudbed1.json
│       │   └── openrca_market_cloudbed2.json
│       └── time_mapping/
│           ├── base_query_parser.py     ← QueryResult dataclass
│           ├── openrca_query_parser.py  ← instruction 파싱
│           └── time_remapper.py         ← 시간 오프셋 계산
│
├── orchestrator/
│   ├── base.py                          ← BaseOrchestrator
│   ├── static_orchestrator.py           ← Static 전용 오케스트레이터
│   ├── evaluators/
│   │   └── openrca_eval.py              ← [NEW] OpenRCA evaluate() 포팅
│   ├── tasks/
│   │   ├── base.py                      ← Task 베이스 (add_result, common_eval)
│   │   └── openrca_task.py              ← [NEW] 통합 OpenRCA task (7개 type 통일)
│   ├── static_problems/
│   │   ├── registry.py                  ← [REWRITE] 동적 문제 생성 (~330개)
│   │   └── openrca/
│   │       ├── base_task.py             ← [REWRITE] query_index 기반
│   │       ├── bank.py                  ← [REWRITE] 단일 OpenRCABankProblem
│   │       ├── telecom.py              ← [REWRITE] 단일 OpenRCATelecomProblem
│   │       ├── market_cloudbed1.py      ← [REWRITE] 단일 OpenRCAMarketCB1Problem
│   │       └── market_cloudbed2.py      ← [REWRITE] 단일 OpenRCAMarketCB2Problem
│   └── static_actions/
│       ├── base.py                      ← get_logs/metrics/traces/exec_shell
│       ├── rca.py                       ← [NEW] 통합 submit (JSON dict)
│       └── detection.py                 ← (하위호환 유지)

aiopslab-applications/static_dataset/
├── docker-compose.yml                   ← [UPDATE] bind mount
├── Dockerfile
└── entrypoint.sh                        ← [UPDATE] 단순화

tests/test_static_dataset.py             ← [REWRITE] 38개 테스트
```

---

## 5. OpenRCA Task Types

| Task | 찾아야 하는 것 | 난이도 | 문제 수 (예시: Bank) |
|------|---------------|--------|---------------------|
| task_1 | datetime | Easy | ~17 |
| task_2 | reason | Easy | ~17 |
| task_3 | component | Easy | ~17 |
| task_4 | datetime + reason | Middle | ~17 |
| task_5 | datetime + component | Middle | ~17 |
| task_6 | component + reason | Middle | ~17 |
| task_7 | datetime + component + reason | Hard | ~18 |

### Agent Submit 형식 (모든 task 공통)

```json
{
  "1": {
    "root cause occurrence datetime": "2021-03-04 14:57:00",
    "root cause component": "Mysql02",
    "root cause reason": "high memory usage"
  }
}
```

- task에서 요구하지 않는 필드는 빈 문자열 가능
- 복수 fault인 경우 "2", "3" 키 추가

### 평가 (openrca_eval.py)

- scoring_points에서 expected 값 regex 추출
- prediction JSON에서 predicted 값 regex 추출
- 복수 fault: `itertools.permutations`로 최적 매칭
- 시간: ±1분 이내 정답 판정
- 최종 점수: `matched_items / total_items` (0.0 ~ 1.0)

---

## 6. 등록된 문제 (~330개)

### 데이터셋별

| 데이터셋 | Config | 문제 수 |
|----------|--------|---------|
| OpenRCA Bank | openrca_bank | ~120 |
| OpenRCA Telecom | openrca_telecom | ~29 |
| OpenRCA Market CB1 | openrca_market_cloudbed1 | ~90 |
| OpenRCA Market CB2 | openrca_market_cloudbed2 | ~90 |

### 문제 ID 형식

```
{dataset_key}-{task_type}-{query_index}
```

예시:
- `openrca_bank-task_1-0` — Bank 데이터셋, task_1, query.csv 0번째 row
- `openrca_bank-task_7-119` — Bank 데이터셋, task_7, query.csv 119번째 row
- `openrca_telecom-task_3-15` — Telecom 데이터셋, task_3, 15번째 row

---

## 7. 데이터 흐름

```
1. StaticOrchestrator.init_problem("openrca_bank-task_1-0")
   │
   ├── Registry → OpenRCABankProblem(query_index=0)
   │   ├── StaticDataset("openrca_bank", query_index=0)
   │   │   ├── config/openrca_bank.json 로드
   │   │   ├── dataset_path 상대경로 해석 → aiopslab-applications/static_dataset/openrca/Bank
   │   │   ├── query.csv[0] → instruction, scoring_points
   │   │   ├── instruction에서 시간 범위 파싱 (OpenRCAQueryParser)
   │   │   └── TimeRemapper 생성 (원본→현재 offset)
   │   └── OpenRCATask(app, query_row, "task_1")
   │       └── scoring_points 저장 (eval에서 사용)
   │
   ├── prob.app.deploy()
   │   ├── Docker Compose (bind mount /tmp/aiopslab-telemetry)
   │   └── _process_and_store_telemetry()
   │       ├── data_mapping에서 telemetry 파일 목록 로드
   │       ├── TimeRemapper로 timestamp 변환
   │       └── /tmp/aiopslab-telemetry/static-bank/ 에 CSV 저장
   │
   ├── Agent 받는 정보:
   │   ├── task_desc: query.csv instruction (시간 변환 반영)
   │   └── actions: get_logs, get_metrics, get_traces, exec_shell, submit

2. Agent 상호작용:
   ├── get_metrics("static-bank", 30) → 변환된 timestamp 기준 필터
   ├── get_logs("static-bank", "Mysql02") → 서비스별 필터
   └── submit({"1": {"root cause occurrence datetime": "...", ...}})

3. 평가:
   └── openrca_evaluate(prediction_str, scoring_points) → score
```

---

## 8. 변경된 파일 목록

### 신규 생성 (3개)

| 파일 | 역할 |
|------|------|
| `orchestrator/evaluators/openrca_eval.py` | OpenRCA evaluate() 포팅 |
| `orchestrator/tasks/openrca_task.py` | 통합 OpenRCA task 클래스 |
| `orchestrator/static_actions/rca.py` | 통합 RCA actions (submit) |

### 전면 재작성 (7개)

| 파일 | 변경 내용 |
|------|----------|
| `orchestrator/static_problems/registry.py` | 하드코딩 7개 → query.csv 동적 ~330개 |
| `orchestrator/static_problems/openrca/base_task.py` | query_index 기반으로 재작성 |
| `orchestrator/static_problems/openrca/bank.py` | 3개 클래스 → 1개 (OpenRCABankProblem) |
| `orchestrator/static_problems/openrca/telecom.py` | Detection만 → OpenRCATelecomProblem |
| `orchestrator/static_problems/openrca/market_cloudbed1.py` | Detection만 → OpenRCAMarketCB1Problem |
| `orchestrator/static_problems/openrca/market_cloudbed2.py` | Detection만 → OpenRCAMarketCB2Problem |
| `tests/test_static_dataset.py` | 19개 → 38개 테스트 |

### 수정 (6개)

| 파일 | 변경 내용 |
|------|----------|
| `service/apps/static_dataset/dataset.py` | query_index 지원 + telemetry 처리 + 상대경로 해석 |
| `orchestrator/static_problems/openrca/__init__.py` | 새 클래스명 export |
| `orchestrator/static_actions/__init__.py` | StaticRCAActions export 추가 |
| `config/openrca_bank.json` | dataset_path 상대경로화 |
| `config/openrca_telecom.json` | dataset_path 상대경로화 |
| `config/openrca_market_cloudbed*.json` | dataset_path 상대경로화 |

### Docker 수정 (2개)

| 파일 | 변경 내용 |
|------|----------|
| `aiopslab-applications/static_dataset/docker-compose.yml` | named volume → bind mount |
| `aiopslab-applications/static_dataset/entrypoint.sh` | 디렉토리 생성만 (처리 로직 제거) |

---

## 9. 추가 구현 필요 사항

| 항목 | 우선순위 | 설명 |
|------|---------|------|
| Alibaba config JSON | 높음 | `config/alibaba_cluster.json` 생성 + query.csv 준비 |
| speed_factor 구현 | 중간 | 현재 정적 CSV 제공 → 실시간 incremental 출력 지원 |
| Docker 통합 테스트 | 중간 | 실제 Docker deploy/cleanup + telemetry 처리 검증 |
| Observer 연동 | 낮음 | 추후 observer 패턴 결정 후 연동 |
| ACME 데이터셋 | 낮음 | ACME config/problem 정의 추가 |
