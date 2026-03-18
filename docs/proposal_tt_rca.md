# Proposal: RCA Agent on RE2-TT Dataset via AIOpsLab

Train Ticket(RE2-TT) 데이터셋을 AIOpsLab의 static-relayer 인프라 위에서 LLM RCA 에이전트로 평가하기 위한 구현 계획입니다.

---

## 목차

1. [전체 아키텍처](#1-전체-아키텍처)
2. [RE2-TT 데이터셋 분석](#2-re2-tt-데이터셋-분석)
3. [구현 계획 (4단계)](#3-구현-계획-4단계)
   - [Step 1: Docker (static-relayer)](#step-1-docker--static-relayer-)
   - [Step 2: Actions](#step-2-actions)
   - [Step 3: Prompt](#step-3-prompt)
   - [Step 4: Client + Run Script](#step-4-client--run-script)
4. [평가 방법](#4-평가-방법)
5. [파일 구조](#5-파일-구조)
6. [기존 코드 재사용 범위](#6-기존-코드-재사용-범위)

---

## 1. 전체 아키텍처

```
RE2-TT Dataset (CSV files)
        │
        ▼
┌──────────────────────────────────┐
│  Docker Container (static-relayer)│  ← Step 1
│  aiopslab-applications/re2tt/    │
│  - Dockerfile                    │
│  - docker-compose.yml            │
│  - entrypoint.sh                 │
│  Mounts: RCAEval/data/RE2/RE2-TT │
└──────────────┬───────────────────┘
               │ docker exec / file mount
               ▼
┌──────────────────────────────────┐
│  Problem Class (RE2TTProblem)    │  ← Step 2 (일부)
│  aiopslab/orchestrator/          │
│  static_problems/re2tt/          │
│  - re2tt_problem.py              │
│  - query.csv  (자동 생성)        │
│                                  │
│  Actions (RE2TTActions)          │  ← Step 2
│  - get_metrics(), get_logs()     │
│  - get_traces(), get_inject_time()│
│  - get_service_topology()        │
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────────┐
│  StaticOrchestrator              │
│  (기존 코드 재사용)               │
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────────┐
│  LLM RCA Agent                   │  ← Step 3, 4
│  clients/react_tt.py             │
│  prompt/tt_rca_prompt.py         │
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────────┐
│  Run Script                      │  ← Step 4
│  clients/run_tt_rca.py           │
│  출력: results/tt_rca/           │
│  - scores.csv (AC@1, AC@3, Avg@5)│
└──────────────────────────────────┘
```

---

## 2. RE2-TT 데이터셋 분석

### 디렉토리 구조

```
RCAEval/data/RE2/RE2-TT/
├── ts-auth-service_cpu/       ← {service}_{fault_type}
│   ├── 1/                     ← run index
│   │   ├── simple_metrics.csv ← 전처리된 메트릭 (service_metrictype 형태)
│   │   ├── metrics.csv        ← 원시 Prometheus 메트릭
│   │   ├── logs.csv           ← (time, timestamp, container_name, message, level, ...)
│   │   ├── logts.csv          ← 로그 이벤트 시계열 (15초 간격)
│   │   ├── traces.csv         ← (traceID, spanID, serviceName, duration, statusCode, ...)
│   │   ├── inject_time.txt    ← Unix 타임스탬프
│   │   └── cluster_info.json  ← 클러스터 토폴로지
│   ├── 2/
│   └── 3/
├── ts-auth-service_delay/
├── ts-order-service_cpu/
└── ...  (총 ~32개 service_fault 조합 × 3 run)
```

### 서비스 목록 (Train Ticket)
`ts-auth-service`, `ts-order-service`, `ts-route-service`, `ts-travel-service`, `ts-user-service`, `ts-seat-service`, `ts-config-service`, `ts-ui-dashboard`

### 장애 유형
| 폴더 접미사 | 장애 내용 | 정답 레이블 |
|---|---|---|
| `_cpu` | CPU 스트레스 | cpu |
| `_mem` | 메모리 스트레스 | mem |
| `_delay` | 네트워크 지연 | latency |
| `_loss` | 패킷 손실 | latency |
| `_disk` | 디스크 I/O | diskio |
| `_socket` | 소켓 고갈 | socket |

### 에이전트에게 제공할 텔레메트리
| 파일 | 제공 방식 | 용도 |
|---|---|---|
| `simple_metrics.csv` | `get_metrics()` | 서비스별 CPU/메모리/레이턴시 시계열 |
| `logs.csv` | `get_logs()` / `search_logs()` | 컨테이너 로그 원문 |
| `traces.csv` | `get_traces()` | 서비스간 요청 추적 |
| `inject_time.txt` | `get_inject_time()` | 장애 주입 시각 |
| `cluster_info.json` | `get_service_topology()` | 서비스 토폴로지 |

---

## 3. 구현 계획 (4단계)

---

### Step 1: Docker (static-relayer)

**목적**: RE2-TT 데이터를 Docker 컨테이너로 서빙하여 기존 `DockerStaticApp` 인터페이스와 호환

**신규 파일**: `aiopslab-applications/re2tt/`

```
aiopslab-applications/re2tt/
├── Dockerfile
├── docker-compose.yml
└── entrypoint.sh
```

#### Dockerfile
```dockerfile
FROM python:3.11-slim

RUN pip install --no-cache-dir pandas

RUN useradd -m -s /bin/bash agent

WORKDIR /app
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# RE2-TT 데이터는 docker-compose에서 볼륨 마운트로 주입
# VOLUME /data/re2tt

ENTRYPOINT ["/app/entrypoint.sh"]
```

#### docker-compose.yml
```yaml
services:
  re2tt-relayer:
    build: .
    container_name: re2tt-static
    volumes:
      # 호스트의 RE2-TT 데이터를 컨테이너 내 /data/re2tt에 마운트
      - ${RE2TT_DATA_PATH:-../../RCAEval/data/RE2/RE2-TT}:/data/re2tt:ro
    environment:
      - CASE_PATH=/data/re2tt   # 어떤 케이스를 서빙할지 런타임에 설정
    stdin_open: true
    tty: true
```

#### entrypoint.sh
```bash
#!/bin/bash
# 컨테이너가 살아있도록 유지 (에이전트가 docker exec으로 파일 읽기)
tail -f /dev/null
```

**핵심 설계 결정**: 기존 OpenRCA 컨테이너는 `process_telemetry.py`로 데이터를 전처리해서 `/agent/telemetry/` 경로에 저장합니다. RE2-TT는 이미 CSV 형태로 존재하므로 **전처리 없이 직접 마운트**합니다. `DockerStaticApp`의 `fetch_*` 메서드가 `docker exec cat` 방식으로 파일을 읽도록 경로만 맞춰줍니다.

---

### Step 2: Actions

**목적**: 에이전트가 RE2-TT 텔레메트리에 접근할 수 있는 API 제공

**신규 파일**:
- `aiopslab/orchestrator/static_problems/re2tt/re2tt_problem.py`
- `aiopslab/orchestrator/static_problems/re2tt/query_generator.py`
- `aiopslab/orchestrator/static_actions/re2tt_actions.py`

#### RE2TTActions (re2tt_actions.py)

기존 `StaticTaskActions`를 상속하되 RE2-TT 특화 액션 추가:

```python
class RE2TTActions(StaticTaskActions):

    def get_inject_time(self, namespace: str) -> str:
        """장애 주입 시각(Unix timestamp)과 사람이 읽기 쉬운 형태를 반환."""
        # inject_time.txt → 에이전트에게 "장애는 이 시각에 주입됨"을 알려줌

    def get_service_topology(self, namespace: str) -> str:
        """cluster_info.json에서 서비스 목록과 pod-node 매핑을 반환."""
        # 에이전트가 어떤 서비스들이 있는지 파악

    def get_metric_anomaly(self, namespace: str) -> str:
        """simple_metrics.csv에서 장애 전후 z-score를 계산해 이상 메트릭 요약."""
        # 에이전트의 탐색 방향 유도
```

#### query.csv 자동 생성 (query_generator.py)

RE2-TT 디렉토리를 스캔해서 query.csv를 생성:

```python
def generate_query_csv(re2tt_root: str) -> pd.DataFrame:
    """
    RE2-TT 폴더 구조를 스캔해 query.csv 형태의 DataFrame 반환.

    출력 컬럼:
        case_id      : ts-auth-service_cpu/1
        service      : ts-auth-service        (정답)
        fault_type   : cpu                    (정답)
        inject_time  : 1705824978             (Unix timestamp)
        data_path    : /data/re2tt/ts-auth-service_cpu/1/
        instruction  : 에이전트에게 줄 문제 설명
        scoring_points: 평가 기준
    """
```

#### RE2TTProblem (re2tt_problem.py)

```python
class RE2TTProblem:
    def __init__(self, case_id: str):
        # case_id = "ts-auth-service_cpu-1" 형태
        # query_generator로 해당 케이스 로드
        # RE2TTActions 초기화
        # Docker 컨테이너와 연결

    def eval(self, submitted_answer: dict) -> dict:
        """
        에이전트가 제출한 답안 평가.
        submitted_answer = {"service": "ts-auth-service", "fault_type": "cpu"}
        반환: {"score": 1.0, "service_correct": True, "fault_correct": True}
        """
```

---

### Step 3: Prompt

**목적**: TT 도메인 지식을 포함한 시스템 프롬프트

**신규 파일**: `prompt/tt_rca_prompt.py`

```python
TT_SYSTEM_PROMPT = """
You are an expert Site Reliability Engineer (SRE) performing Root Cause Analysis
on the Train Ticket microservice system running on Kubernetes.

## System Overview
Train Ticket is an online railway ticket booking system with these microservices:
- ts-auth-service       : User authentication
- ts-order-service      : Order management
- ts-route-service      : Route information
- ts-travel-service     : Train schedule management
- ts-user-service       : User profile management
- ts-seat-service       : Seat availability
- ts-config-service     : System configuration
- ts-ui-dashboard       : Frontend / SLI entry point

## Available Telemetry
{telemetry_apis}

## Shell Access
{shell_api}

## Submission
{submit_api}

## Possible Root Causes
{possible_root_causes}

## Analysis Strategy
1. Start with get_inject_time() to know when the fault occurred.
2. Use get_metric_anomaly() for a quick anomaly overview.
3. Drill into specific services with get_metrics() / get_logs() / get_traces().
4. Compare pre-fault vs post-fault behavior.
5. Submit your answer with the root cause service and fault type.
"""

TT_TASK_PROMPT = """
A fault was injected into the Train Ticket microservice system.
The system exhibited abnormal behavior after the fault injection.

Your task: Identify the ROOT CAUSE SERVICE and FAULT TYPE.

Fault types to consider: cpu, mem, delay (network latency), loss (packet loss),
disk (disk I/O stress), socket (socket exhaustion).

Submit your answer as:
  service: <service_name>
  fault_type: <fault_type>
"""
```

---

### Step 4: Client + Run Script

**목적**: 에이전트를 RE2-TT 문제에 연결하고 실험 실행

**신규 파일**:
- `clients/react_tt.py` — ReAct 에이전트 (react_static.py 패턴 재사용)
- `clients/run_tt_rca.py` — 실험 실행 스크립트

#### react_tt.py 구조

```python
class TTAgent:
    """RE2-TT 전용 ReAct 에이전트. react_static.py와 동일한 패턴."""

    def __init__(self, model="claude-sonnet-4-6"):
        self.history = []
        self.llm = AnthropicClient(model=model)   # or GPTClient

    def init_context(self, problem_desc, instructions, apis, inject_time, topology):
        # TT_SYSTEM_PROMPT 포매팅
        # inject_time, topology를 컨텍스트에 포함

    async def get_action(self, observation: str) -> str:
        # ReAct 루프: Thought → Action
```

#### run_tt_rca.py 구조

```python
"""
Usage:
    # 전체 RE2-TT 데이터셋 실행
    python clients/run_tt_rca.py --dataset re2-tt

    # 특정 장애 유형만 실행
    python clients/run_tt_rca.py --fault-type cpu

    # 단일 케이스 실행
    python clients/run_tt_rca.py --case ts-auth-service_cpu-1

    # 스모크 테스트 (2케이스)
    python clients/run_tt_rca.py --test
"""

def main():
    # 1. RE2-TT 케이스 목록 로드 (query_generator)
    # 2. Docker 컨테이너 시작 (re2tt-static)
    # 3. 각 케이스에 대해:
    #    a. RE2TTProblem 초기화
    #    b. TTAgent 초기화 + init_context
    #    c. StaticOrchestrator.start_problem()
    #    d. 결과 평가 및 저장
    # 4. 전체 결과 집계 (AC@1, AC@3, Avg@5 — service/metric 두 수준)
```

---

## 4. 평가 방법

### 에이전트 제출 형식

```
service: ts-auth-service
fault_type: cpu
```

### 점수 산정

RCAEval 논문과 동일한 지표를 두 수준에서 계산:

| 지표 | 수준 | 설명 |
|---|---|---|
| AC@1 | Service | 1순위 예측 서비스가 정답 서비스와 일치 |
| AC@1 | Metric | 1순위 예측 (service, fault_type) 쌍이 정답과 일치 |
| Avg@5 | Service | AC@1~AC@5의 평균 (서비스 수준) |
| Avg@5 | Metric | AC@1~AC@5의 평균 (메트릭 수준) |

> LLM 에이전트는 ranking이 아닌 단일 답변을 제출하므로 AC@1이 주요 지표

### 결과 저장 형식

```
results/tt_rca/{eval_id}_scores.csv
```

```
timestamp, eval_id, model, case_id, service, fault_type,
pred_service, pred_fault, service_correct, fault_correct,
score, steps, TTA, in_tokens, out_tokens
```

---

## 5. 파일 구조

구현 후 추가/수정될 파일:

```
AIOpsLab/
├── aiopslab-applications/
│   └── re2tt/                              [NEW]
│       ├── Dockerfile
│       ├── docker-compose.yml
│       └── entrypoint.sh
│
├── aiopslab/
│   └── orchestrator/
│       ├── static_actions/
│       │   └── re2tt_actions.py            [NEW]
│       └── static_problems/
│           └── re2tt/                      [NEW]
│               ├── __init__.py
│               ├── re2tt_problem.py
│               └── query_generator.py
│
├── prompt/
│   └── tt_rca_prompt.py                    [NEW]
│
└── clients/
    ├── react_tt.py                         [NEW]
    └── run_tt_rca.py                       [NEW]
```

수정 파일:
```
aiopslab/orchestrator/static_problems/registry.py  [MODIFY: RE2-TT 등록]
```

---

## 6. 기존 코드 재사용 범위

| 기존 컴포넌트 | 재사용 방식 |
|---|---|
| `StaticOrchestrator` | 그대로 사용 — 에이전트-환경 루프 전부 처리 |
| `StaticTaskActions` | 상속 — `get_metrics`, `get_logs`, `get_traces`, `exec_shell` 재사용 |
| `react_static.py` | 패턴 복사 — `Agent` 클래스, `setup_executor`, `append_score` 동일 구조 |
| `DockerStaticApp` | 그대로 사용 — `docker exec cat` 방식으로 파일 접근 |
| `StaticProblemRegistry` | RE2-TT 등록 후 재사용 |
| `Evaluator` (RCAEval) | Avg@5, AC@k 계산에 재사용 |

**신규 작성 필요**:
- RE2-TT 전용 Docker 설정 (OpenRCA와 데이터 구조 다름)
- `query_generator.py` (RE2-TT 폴더 → query.csv 자동 변환)
- `RE2TTProblem.eval()` (service + fault_type 두 수준 평가)
- TT 도메인 프롬프트
- `get_inject_time()`, `get_service_topology()`, `get_metric_anomaly()` 액션

---

## 구현 순서 요약

```
[Step 1] Docker
  → aiopslab-applications/re2tt/ 생성
  → docker-compose up 테스트

[Step 2] Actions + Problem
  → query_generator.py 작성 (RE2-TT 폴더 → query.csv)
  → RE2TTActions 작성 (get_inject_time, get_service_topology, get_metric_anomaly)
  → RE2TTProblem 작성 (eval 포함)
  → registry.py에 re2tt 등록

[Step 3] Prompt
  → tt_rca_prompt.py 작성
  → 시스템/태스크 프롬프트 정의

[Step 4] Client + Run Script
  → react_tt.py 작성 (TTAgent)
  → run_tt_rca.py 작성 (실험 실행 + 결과 저장)
  → 스모크 테스트 실행
```
