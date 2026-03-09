# RCA Agent Experiment: 3-Stage Diagnosis Architecture

## Overview

30개 task (Telecom 10, Market 10, Bank 10)에 대해 다양한 RCA agent를 구현하고 성능을 비교하는 실험.
모든 agent는 공통 3단계 프레임워크를 따르며, 본 실험은 **Stage 2 (Deep Dive), Stage 3 (Expand)에 집중**한다.
Stage 1 (Localization)의 후보 3개는 사전 분석으로 고정 제공된다.

## 3-Stage Framework

```
┌──────────────────────────────────────────────────────────┐
│                     EXPLORATION                          │
│  시스템 구조 파악: 컴포넌트 목록, 호출 관계, KPI 종류     │
│  output: 시스템 전체 그림 (topology, 정상 baseline)       │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│                    LOCALIZATION                           │
│  이상치 분석으로 의심 후보 3개 선정                        │
│  output: ranked candidates [(T1,C1), (T2,C2), (T3,C3)]  │
│  ※ 본 실험에서는 사전 분석 결과를 고정 입력으로 사용       │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
              candidates 순회 (rank 순)
                       │
                       ▼
         ┌─────────────────────────────┐
         │         DEEP DIVE           │◄──────────────┐
         │  (T, C) 집중 검증            │               │
         │  · spike vs real fault 판별  │               │
         │  · reason 결정               │               │
         └──────────┬──────────────────┘               │
                    │                                   │
              ┌─────┴─────┐                             │
              │           │                             │
         [rejected]    [confirmed]                      │
              │           │                             │
              ▼           ▼                             │
         다음 후보     ┌──────────┐                     │
         로 이동       │  EXPAND   │                     │
         (없으면       │  더 깊은   │────────────────────┘
          최고 conf.   │  root 탐색 │  새 (T,C) 발견 → Deep Dive
          후보 제출)   └─────┬────┘
                             │
                        [없음: 확정]
                             │
                             ▼
                      Root Cause 제출
                      (T, C, Reason)
```

### 종료 조건

1. **Expand에서 더 깊은 root가 없음** → 현재 (T, C, R) 확정 후 제출
2. **Expand → Deep Dive에서 새 후보가 rejected** → Expand 이전의 confirmed 후보를 root로 확정
3. **모든 후보 rejected** → 가장 높은 confidence의 후보를 제출 (fallback)

---

## Stage 상세

### Stage 0: Exploration

**목적**: agent가 진단 시작 전 시스템의 전체적인 그림을 파악

**수행 내용**:
- 시스템 컴포넌트 목록 및 유형 파악 (web server, app server, DB, cache, gateway 등)
- 컴포넌트 간 호출 관계 (topology) 파악
- 사용 가능한 KPI 종류 확인
- 정상 상태 baseline 감각 형성

**사용 가능 action**:
- `get_kpi_deviation_table()` - 전체 컴포넌트 요약 (정상 baseline 파악)
- `get_trace_call_graph()` - 호출 관계 topology
- `get_log_overview()` - 로그 구조 파악
- `execute()` - 커스텀 탐색 코드

**output**: 시스템 이해 context (이후 모든 stage에서 참조)

---

### Stage 1: Localization

**목적**: 전체 telemetry에서 이상치가 의심되는 (time, component) 후보 3개를 선정

**사용 가능 action**:
- `get_kpi_high_deviation()` - P90 초과 metric 컴포넌트 조회
- `get_kpi_deviation_table()` - 전체 컴포넌트 요약 테이블
- `get_trace_call_graph()` - trace 기반 이상 signal
- `get_anomaly_metrics()` - log 기반 이상 서비스

**output**: ranked candidates 3개 `[(T1,C1), (T2,C2), (T3,C3)]`

**기준**: z-score, deviation ratio, fail_rate 등 정량 지표 기반

> **본 실험에서는** `experiments/bank_top3_candidates.json` 등 사전 분석 결과를 고정 입력으로 사용.
> Localization 자체의 성능은 별도 실험에서 다룸.

---

### Stage 2: Deep Dive

**목적**: 선택된 (T, C)가 단순 spike인지 실제 fault인지 검증하고, **reason을 결정**

**사용 가능 action**:
- `get_component_kpi_deviation()` - 특정 컴포넌트 상세 metric
- `execute()` - 커스텀 분석 코드 (시계열 패턴, 상관관계)
- `search_logs()` - 해당 시간/컴포넌트 error log 검색
- `get_trace_call_graph()` - 해당 컴포넌트의 caller/callee trace 분석

**검증 관점**:
1. 해당 KPI가 해당 시간대에만 이상인가? (일시적 spike vs 지속적 이상)
2. 다른 telemetry source에서도 이상이 확인되는가? (metric + log + trace 교차 검증)
3. 해당 컴포넌트의 다른 KPI에도 영향이 있는가? (단일 KPI spike vs 전반적 이상)

**output**:
- 판정: `confirmed` (이상 확인) / `rejected` (정상 또는 spike)
- confirmed인 경우: reason 결정 (e.g., "high CPU usage", "network latency")
- confidence: `high` / `medium` / `low`
- evidence: 근거가 된 telemetry 데이터 요약

---

### Stage 3: Expand

**목적**: Deep Dive에서 확인된 이상의 더 근본적인 원인이 있는지 탐색

**탐색 방향**:
- **Upstream 탐색**: trace call graph에서 해당 컴포넌트를 호출하는 상위 컴포넌트 확인
- **Downstream 탐색**: 해당 컴포넌트가 의존하는 하위 서비스 확인
- **시간 선행 분석**: 현재 이상 시점보다 먼저 이상이 발생한 컴포넌트 확인

**판단 기준**: (구체화 예정)

**output**:
- 새 (T, C) 발견 → Deep Dive로 이동
- 발견 없음 → 현재 (T, C, R) 확정

---

## Diagnosis Tree (State 관리)

각 agent는 진단 과정을 **tree 구조**로 관리한다.
LLM이 읽고 쓰기 쉬운 indented text 형식을 사용한다.

### 형식

```
DIAGNOSIS TREE
==============
system: {dataset_name} | window: {start} ~ {end}
topology: {컴포넌트 요약 from exploration}

[1] C=Tomcat02 T=19:22 | status=CONFIRMED | reason=network latency | conf=high
    evidence: "trace elapsed 3200ms (baseline 45ms), network_gap_ratio=0.89"
    ├── [1.1] C=MG01 T=19:20 | status=CONFIRMED | reason=network latency | conf=medium
    │   evidence: "caller of Tomcat02, elapsed spike at 19:20 (2ms before)"
    │   └── [1.1.1] C=IG01 T=19:19 | status=REJECTED | reason=- | conf=-
    │       evidence: "upstream of MG01 but metrics normal, no preceding anomaly"
    └── EXPAND: no deeper root found → ROOT = [1.1] MG01

[2] C=Redis02 T=19:25 | status=SKIPPED (root found at [1.1])

[3] C=apache01 T=19:30 | status=SKIPPED (root found at [1.1])

RESULT: C=MG01 T=19:20 R=network latency
```

### 필드 설명

| 필드 | 설명 |
|------|------|
| `[depth.order]` | 트리 위치 (1=후보1, 1.1=후보1의 expand 결과, 1.1.1=더 깊은 expand) |
| `C` | Component |
| `T` | Time (이상 발생 시점) |
| `status` | `CONFIRMED` / `REJECTED` / `SKIPPED` / `PENDING` |
| `reason` | Deep Dive에서 결정된 fault reason (confirmed일 때만) |
| `conf` | confidence: `high` / `medium` / `low` |
| `evidence` | 근거 telemetry 요약 (1줄) |
| `ROOT` | 최종 확정된 root cause node |

### Tree 동작 규칙

1. **순회 순서**: 후보를 rank 순서대로 `[1]` → `[2]` → `[3]` 진행
2. **Deep Dive 결과**:
   - `CONFIRMED` → Expand 단계 진입, 하위 노드 탐색
   - `REJECTED` → 다음 후보로 이동
3. **Expand 결과**:
   - 새 (T, C) 발견 → 하위 노드 `[n.m]` 추가 후 Deep Dive
   - 발견 없음 → 현재 노드를 ROOT로 확정
4. **ROOT 확정 시**: 나머지 후보는 `SKIPPED` 처리
5. **전체 REJECTED 시**: 가장 높은 confidence 후보를 ROOT로 선택 (fallback)

### LLM 사용 방식

- **읽기**: 매 step 시작 시 현재 tree 상태를 system prompt에 포함
- **쓰기**: agent가 `update_tree(node_id, status, reason, conf, evidence)` action으로 갱신
- **조회**: `get_tree()` action으로 현재 전체 tree 확인

---

## Context 관리: 3-Layer Architecture

### 기존 방식의 문제

기존 agent는 **단일 flat history**에 모든 대화를 축적하고, `_trim_history()`로 뒤에서부터 120k 토큰만 유지한다.

```
[system] [user] [assistant] [user] [assistant] ... [user] [assistant]
 프롬프트   피드백    응답      피드백    응답    ...  피드백    응답
                                                 ←── 120k tokens ──→
```

| 문제 | 설명 |
|------|------|
| 초반 정보 유실 | Exploration에서 파악한 topology, baseline이 trim으로 잘림 |
| Context 낭비 | node [1]의 raw executor output이 node [1.1] 분석 시에도 남아있음 |
| Stage 혼란 | LLM이 현재 어떤 stage, 어떤 node를 분석 중인지 맥락을 잃음 |

### 3-Layer 구조

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: PERSISTENT CONTEXT (항상 유지, 절대 trim 안 됨)    │
│                                                             │
│  · System Prompt        규칙, 가용 action 목록               │
│  · System Understanding Exploration 결과 요약               │
│  · Diagnosis Tree       현재 tree 상태 전문                  │
│  · Current Stage        현재 stage & node 표시               │
│                                                             │
│  예상 크기: ~3-5k tokens                                    │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: STAGE SUMMARY (완료된 node 요약, 축적)             │
│                                                             │
│  · 완료된 각 node의 1-paragraph 요약                         │
│  · 핵심 수치와 판단 근거만 포함                               │
│                                                             │
│  예상 크기: ~200 tokens/node, 총 ~1-2k tokens               │
├─────────────────────────────────────────────────────────────┤
│  Layer 3: WORKING MEMORY (현재 node의 raw 대화)              │
│                                                             │
│  · 현재 진행 중인 node의 action 호출 및 결과 원문             │
│  · executor 결과, trace/metric/log raw 데이터                │
│  · stage 전환 시 초기화 (요약 후 Layer 2로 이동)              │
│                                                             │
│  예상 크기: ~20-80k tokens (가변)                            │
└─────────────────────────────────────────────────────────────┘

총 사용량: ~25-87k tokens (120k 한도 내 충분한 여유)
```

### 매 LLM 호출 시 messages 구성

```python
messages = [
    # ── Layer 1: Persistent (system prompt) ──
    {"role": "system", "content": """
{SYSTEM_RULES}
{ACTION_LIST}

## System Understanding
{self.system_understanding}
# ← Exploration 결과 요약 (topology, 컴포넌트 목록, baseline 등)

## Diagnosis Tree
{self.tree.render()}
# ← 현재 tree 전문 (status, reason, evidence 포함)

## Current Stage
Stage: {self.current_stage}  |  Node: {self.current_node}
# ← 지금 agent가 어떤 단계의 어떤 node를 작업 중인지 명시
"""},

    # ── Layer 2: Completed node summaries ──
    {"role": "user", "content": """
## Completed Analysis
{self.completed_summaries}
# ← 완료된 node들의 1-paragraph 요약 목록
"""},

    # ── Layer 3: Working memory (현재 node raw 대화) ──
    *self.working_memory,
    # ← 현재 node에서의 action 호출/결과 원문
]
```

### Stage 전환 시 Context 관리 흐름

```
[현재 node 작업 완료]
        │
        ▼
┌─────────────────────────────────────────┐
│  1. SUMMARIZE                           │
│     working_memory → LLM에게 요약 요청   │
│     "1-paragraph로 핵심 수치와 판단 근거  │
│      요약해줘"                           │
│                                         │
│  예시 출력:                              │
│  "Node [1] Tomcat02: CONFIRMED.          │
│   trace elapsed 3200ms (baseline 45ms),  │
│   network_gap_ratio=0.89, error log      │
│   'connection timeout' at 19:22:15.      │
│   Reason: network latency, conf=high"    │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│  2. ARCHIVE                             │
│     요약을 completed_summaries에 추가     │
│     (Layer 2에 축적)                     │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│  3. RESET                               │
│     working_memory 초기화 (비움)          │
│     (Layer 3 클리어)                     │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│  4. UPDATE TREE                         │
│     Diagnosis Tree 노드 상태 갱신         │
│     (Layer 1에 반영)                     │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│  5. NEXT STAGE                          │
│     current_stage, current_node 업데이트  │
│     새 working_memory 축적 시작           │
└─────────────────────────────────────────┘
```

### 전체 진단 과정 예시

```
Step 1-3: EXPLORATION
  Layer 1: system prompt + "Stage: EXPLORATION"
  Layer 3: topology 조회, KPI 목록 확인 등 raw 대화
  ──── stage 전환 ────
  → Layer 1에 system_understanding 저장 (topology 요약)
  → Layer 3 초기화

Step 4-8: DEEP DIVE [1] (Tomcat02, T=19:22)
  Layer 1: system prompt + system_understanding + tree + "Stage: DEEP_DIVE [1]"
  Layer 3: metric 조회, log 검색, trace 분석 raw 대화
  ──── stage 전환 (CONFIRMED) ────
  → Layer 2에 node [1] 요약 추가
  → Tree [1] status=CONFIRMED 갱신
  → Layer 3 초기화

Step 9-11: EXPAND [1] → DEEP DIVE [1.1] (MG01, T=19:20)
  Layer 1: tree (node [1] confirmed 포함) + "Stage: DEEP_DIVE [1.1]"
  Layer 2: "Node [1] Tomcat02: confirmed, network latency, ..."
  Layer 3: MG01 상세 분석 raw 대화
  ──── stage 전환 (CONFIRMED, no deeper root) ────
  → Layer 2에 node [1.1] 요약 추가
  → Tree [1.1] = ROOT 확정
  → 나머지 후보 SKIPPED

Step 12: SUBMIT
  Layer 1: 최종 tree (ROOT = [1.1] MG01)
  → submit(T=19:20, C=MG01, R=network latency)
```

### 기존 방식 vs 3-Layer 비교

| 항목 | 기존 (flat history) | 3-Layer |
|------|-------------------|---------|
| Exploration 결과 | trim으로 유실 가능 | Layer 1에 영구 보존 |
| 완료 node 정보 | raw 대화가 context 차지 | 1-paragraph 요약으로 압축 |
| Stage 인식 | LLM이 대화 흐름에서 추론 | `Current Stage: DEEP_DIVE [1.1]` 명시 |
| Tree 상태 | 없음 | 매 호출마다 최신 tree 포함 |
| Context 효율 | 120k 중 대부분이 과거 raw | 과거=요약, raw=현재 작업만 |
| 이전 node 참조 | raw 대화 스크롤 | Layer 2 요약으로 즉시 참조 |

### 구현 요소

| 요소 | 설명 | 위치 |
|------|------|------|
| `DiagnosisTree` | tree 관리 클래스 (render, update, get_root) | `staged_rca_agent.py` |
| `system_understanding` | Exploration 결과 요약 문자열 | agent 인스턴스 변수 |
| `completed_summaries` | 완료 node 요약 리스트 | agent 인스턴스 변수 |
| `working_memory` | 현재 node raw 대화 리스트 | agent 인스턴스 변수 |
| `_summarize_node()` | working_memory → 1-paragraph 요약 LLM 호출 | agent 메서드 |
| `_build_messages()` | 3-layer 조합하여 messages 리스트 생성 | agent 메서드 |

---

## Agent Variants

| Agent ID | 설명 | 차별점 |
|----------|------|--------|
| `staged-basic` | 기본 3단계 agent | LLM이 단계 전환 직접 판단 |
| `staged-critic` | 3단계 + critic layer | 각 단계 결론에 critic 검증 추가 |
| `staged-structured` | 3단계 + 구조화된 action 제한 | 각 단계에서 사용 가능 action을 제한 |
| (추가 예정) | | |

## 실험 설정

### 대상 Task

`experiments/problems_30.txt` 기준 30개 task (모두 task_7: Time + Component + Reason)

| Dataset | 개수 | Indices |
|---------|------|---------|
| Telecom | 10 | 0, 2, 4, 5, 8, 9, 13, 19, 20, 27 |
| Market CB1 | 10 | 2, 8, 10, 12, 14, 24, 29, 30, 56, 59 |
| Bank | 10 | 0, 1, 3, 6, 8, 47, 48, 55, 60, 68 |

### 평가 지표

| Metric | 설명 |
|--------|------|
| `success_rate` | score == 1.0 비율 |
| `t_rate` | Time 정답률 |
| `c_rate` | Component 정답률 |
| `r_rate` | Reason 정답률 |
| `avg_score` | 평균 점수 |
| `avg_steps` | 평균 step 수 |
| `avg_TTA` | 평균 소요 시간 (초) |
| `avg_tokens` | 평균 토큰 사용량 (in + out) |

### 실행 방법

```bash
python clients/run_experiment.py \
  --problems-file experiments/problems_30.txt \
  --agent staged-basic \
  --api-config clients/openrca_rca/api_config.yaml \
  --eval-id staged-basic-gpt5
```

## 결과 기록

### 실험 결과 테이블

| Experiment | Agent | Model | success_rate | t_rate | c_rate | r_rate | avg_steps | avg_TTA | Notes |
|------------|-------|-------|-------------|--------|--------|--------|-----------|---------|-------|
| | | | | | | | | | |

### 관찰 및 분석

(실험 후 작성)

## 파일 구조

```
clients/openrca_rca/
    staged_rca_agent.py          # 3-stage agent 구현
    prompts/
        localization_prompt.py   # Stage 1 프롬프트
        deepdive_prompt.py       # Stage 2 프롬프트
        expand_prompt.py         # Stage 3 프롬프트
experiments/
    AGENT_EXPERIMENT.md          # 본 문서
    problems_30.txt              # 대상 task 목록
    bank_top3_candidates.json    # Bank 고정 후보 (사전 분석)
results/experiments/
    {eval_id}/
        scores.csv
        summary.txt
```
