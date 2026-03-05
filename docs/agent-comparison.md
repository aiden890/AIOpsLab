# Agent Implementation Comparison: Paper vs AIOpsLab

This document compares the theoretical approach from papers with actual implementation in AIOpsLab.

---

## ReAct Agent

### Paper Reference

| Field | Value |
|-------|-------|
| Title | ReAct: Synergizing Reasoning and Acting in Language Models |
| Authors | Yao, S., Zhao, J., Yu, D., Du, N., Shafran, I., Narasimhan, K., & Cao, Y. |
| Year | 2022 |
| Paper | https://arxiv.org/abs/2210.03629 |

### Core Concept (Paper)

Interleave reasoning traces (Thought) and task-specific actions (Action) in a synergistic loop. Reasoning helps the model induce, track, and update action plans, while actions allow it to interface with external sources to gather information.

### Loop Structure (Paper)

```
┌─────────────────────────────────────────────────────────┐
│                    ReAct Loop                           │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   ┌──────────┐                                         │
│   │ Thought  │ ← Reason about current situation        │
│   └────┬─────┘                                         │
│        │                                               │
│        ▼                                               │
│   ┌──────────┐                                         │
│   │  Action  │ ← Execute action in environment        │
│   └────┬─────┘                                         │
│        │                                               │
│        ▼                                               │
│   ┌──────────┐                                         │
│   │Observation│ ← Receive feedback from environment   │
│   └────┬─────┘                                         │
│        │                                               │
│        └──────────────► Repeat until solved            │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### Example Trace (Paper)

```
Thought: I need to find the capital of France
Action: Search[capital of France]
Observation: Paris is the capital of France
Thought: I found the answer, I should finish
Action: Finish[Paris]
```

### Key Features (Paper)

| Feature | Description |
|---------|-------------|
| Explicit Reasoning Traces | Model explicitly states its reasoning before acting |
| Action Grounding | Actions are grounded in reasoning, not random exploration |
| Error Recovery | Reasoning allows model to recognize and recover from errors |
| Interpretability | Human can understand agent's decision-making process |

---

### AIOpsLab Implementation

**File**: `clients/react.py`

#### What It Actually Does

```python
# The only "ReAct" logic is this prompt instruction:
RESP_INSTR = """DO NOT REPEAT ACTIONS! Respond with:
Thought: <your thought on the previous output>
Action: <your action towards mitigating>
"""

# And adding it to each input:
def _add_instr(self, input):
    return input + "\n\n" + RESP_INSTR
```

#### Code Flow

```
Input from environment
        │
        ▼
Append RESP_INSTR ("Respond with Thought/Action")
        │
        ▼
Trim history to token limit (tiktoken)
        │
        ▼
Send to LLM
        │
        ▼
Return raw response (NO PARSING)
```

#### What Is Missing

| Missing Feature | Description |
|-----------------|-------------|
| ❌ No Thought/Action Parsing | Response is not parsed to extract Thought vs Action separately |
| ❌ No Format Validation | No check if LLM actually followed Thought/Action format |
| ❌ No Observation Labeling | Environment output is not explicitly labeled as "Observation" |
| ❌ No Reasoning Chain Tracking | No separate storage/analysis of reasoning traces |
| ❌ No Error Detection | No detection of reasoning errors or action failures |

---

## FLASH Agent

### Paper Reference

> Note: The implementation comment says "naive implementation of Flash without tool and TSG". This appears to be inspired by hindsight learning concepts.

### Core Concept

Uses hindsight (retrospective analysis) to improve decision-making. Generates additional guidance by analyzing past actions and current state before making the next decision.

### Key Features (Conceptual)

| Feature | Description |
|---------|-------------|
| Hindsight Generation | Retrospective analysis of past actions to guide future |
| Status Supervision | Monitor current state to inform decisions |
| Two-Stage Reasoning | First generate hindsight, then decide action |

---

### AIOpsLab Implementation

**File**: `clients/flash.py`

#### Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    FLASH Agent                          │
├─────────────────────────────────────────────────────────┤
│                                                         │
│   ┌────────────────────────────────────────────────┐   │
│   │              FlashAgent                         │   │
│   │  - history: conversation history                │   │
│   │  - llm: GPTClient                              │   │
│   │  - hindsight_builder: HindsightBuilder         │   │
│   └────────────────────────────────────────────────┘   │
│                         │                               │
│                         ▼                               │
│   ┌────────────────────────────────────────────────┐   │
│   │           HindsightBuilder                      │   │
│   │  - summarize_history(): last 5 messages        │   │
│   │  - generate_prompt(): create hindsight prompt  │   │
│   │  - develop_hindsight(): LLM call for guidance  │   │
│   └────────────────────────────────────────────────┘   │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

#### Code Flow

```
Input from environment
        │
        ▼
Trim history for hindsight (50k tokens)
        │
        ▼
┌───────────────────────────────────┐
│  HindsightBuilder (LLM Call #1)   │
│  "Should next action be submit?   │
│   If not, suggest diagnostics."   │
└───────────────────────────────────┘
        │
        ▼
Combine input + hindsight
        │
        ▼
Trim combined history
        │
        ▼
┌───────────────────────────────────┐
│     Main LLM (LLM Call #2)        │
│     Generate final action         │
└───────────────────────────────────┘
        │
        ▼
Return response
```

#### Hindsight Prompt

```
You are a helpful assistant determining the next best action...

Given the history of the previous actions:
{summarized_history}  # Last 5 messages, 300 chars each

And the environment output from last action:
{input}

1. Should the next action be a submit operation?
2. If not, please suggest additional diagnostic steps.

Thought: Identify whether submitting is the right next step.
Solution: Provide reasoning and next steps.
```

#### Bugs in Implementation

| Bug | Location | Description |
|-----|----------|-------------|
| 🐛 Typo | Line 94 | `hightsight = hindsight[:1000]` - typo and variable unused |
| 🐛 Missing Return | `diagnose_with_hindsight()` | Method doesn't return hindsight value |
| 🐛 No TSG | Comment | "without tool and TSG" - Troubleshooting Guide not implemented |

---

## Comparison Summary

### ReAct vs FLASH

| Aspect | ReAct | FLASH |
|--------|-------|-------|
| LLM calls per step | 1 | 2 |
| Uses hindsight | ❌ | ✅ |
| Prompt-based reasoning | ✅ | ✅ |
| Code-level reasoning | ❌ | Partial |
| APIs available | Shell + Telemetry | Shell + Telemetry |

### Both Are Missing

| Missing Feature | Impact |
|-----------------|--------|
| Proper action parsing and validation | Can't detect malformed responses |
| Structured observation handling | Environment output not labeled |
| Error recovery mechanisms | Agent can't recover from mistakes |
| Reasoning chain analysis | Can't analyze decision quality |

### Paper vs Implementation

```
┌─────────────────────────────────────────────────────────────────┐
│                    Implementation Gap                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   Paper ReAct                    AIOpsLab react.py              │
│   ──────────                     ────────────────               │
│   Thought → Parse → Store        Just prompt: "write Thought:"  │
│   Action → Parse → Execute       Raw text to orchestrator       │
│   Observation → Label → Add      Raw env output appended        │
│   Error → Detect → Recover       No error handling              │
│                                                                  │
│   Paper FLASH                    AIOpsLab flash.py              │
│   ───────────                    ────────────────               │
│   TSG Integration                Not implemented                │
│   Status Supervision             Partial (hindsight only)       │
│   Hindsight Learning             Basic (2-stage LLM call)       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## ReAct + Critic Agent

### 핵심 컨셉

ReAct agent에 Stateless Critic을 추가하여, Controller의 observation을 raw executor 결과와 비교 검증하는 구조. 매 step마다 Critic이 누락된 anomaly, 잘못된 해석, 편향된 분석을 감지하는 것이 목표.

### 아키텍처

```
┌─────────────────────────────────────────────────────────┐
│                 ReAct + Critic Loop                      │
├─────────────────────────────────────────────────────────┤
│                                                          │
│   ┌──────────┐                                          │
│   │Instruction│ ← Controller가 다음 step 지시 생성      │
│   └────┬─────┘                                          │
│        ▼                                                │
│   ┌──────────┐                                          │
│   │ Executor │ ← IPython 커널에서 코드 실행             │
│   └────┬─────┘                                          │
│        ▼ raw_result                                     │
│   ┌──────────────┐                                      │
│   │  Analysis    │ ← Controller가 raw result 해석       │
│   │  (Phase 1)   │    (전체 히스토리 보유)               │
│   └────┬─────────┘                                      │
│        ▼ observation                                    │
│   ┌──────────────┐                                      │
│   │   Critic     │ ← Stateless: raw_result vs analysis  │
│   │  (Phase 2)   │    (매번 새 messages 생성)            │
│   └────┬─────────┘                                      │
│        ▼ 수정된 analysis (이슈 발견 시)                  │
│   ┌──────────────┐                                      │
│   │ Instruction  │ ← 수정된 analysis 기반 다음 지시 생성 │
│   │  (Phase 3)   │                                      │
│   └────┬─────────┘                                      │
│        └──────────────► submit까지 반복                  │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### 주요 파일

| 파일 | 역할 |
|------|------|
| `clients/openrca_rca/react_rca_critic_agent.py` | 3-phase agent: analysis → critic → instruction |
| `clients/openrca_rca/prompts/critic_prompt.py` | Critic 시스템 프롬프트 + 유저 템플릿 |
| `clients/run_react_rca_critic.py` | 배치 실험 실행기 |

### 현재 Critic 프롬프트

```python
CRITIC_SYSTEM_PROMPT = """
You are a Critic agent for root cause analysis.

Your role:
- Controller의 observation을 raw executor result와 비교
- Controller가 놓치거나 잘못 해석한 anomaly, component, 패턴 식별
- instruction이나 action은 생성하지 않음 — observation 검증만 수행

검증 규칙:
1. missed_anomaly: raw result에 있지만 observation에 없는 anomaly
2. wrong_interpretation: observation이 실제 데이터와 모순
3. biased_focus: 하나의 component만 주목하고 동등/더 높은 심각도의 다른 것 무시
"""
```

---

### 실험 결과: Telecom 데이터셋 (2026-03-04)

**설정**: eval_id=6892c482, Telecom 51문제, model=gpt-5, max_steps=40

#### 전체 성능 비교

| 지표 | Original (ReAct) | Critic (ReAct+Critic) |
|------|-------------------|----------------------|
| 평균 점수 | **0.212** | 0.176 |
| 성공률 (score>0) | **27.5%** (14/51) | 25.5% (13/51) |
| 만점 (1.0) | 6 | 7 |
| 부분 점수 | 8 | 6 |
| 실패 (0.0) | **37** | 38 |

**결론**: Critic agent가 전체적으로 성능이 더 낮음. 평균 점수 0.212 → 0.176으로 하락.

#### Regression: 원본 성공 → Critic 실패 (9건)

| idx | Task | 정답 | 원본 점수 | Critic 점수 | Steps (원→Critic) | 실패 유형 |
|-----|------|------|----------|------------|-------------------|----------|
| 3 | task_2 | docker_002 / CPU fault | 1.0 | 0.0 | 7→36 | 분석 루프 + 이유 오판 |
| 11 | task_6 | db_003 / db connection limit | 0.5 | 0.0 | 7→4 | 조기 제출 |
| 16 | task_7 | db_007 / db close | 1.0 | 0.33 | 10→5 | 이유 오판 (network delay) |
| 17 | task_2 | docker_003 / CPU fault | 1.0 | 0.0 | 9→10 | 유효한 발견 후 이탈 |
| 24 | task_2 | os_021 / network delay | 1.0 | 0.0 | 6→37 | 분석 루프 |
| 25 | task_4 | os_018 / network loss | 0.5 | 0.0 | 9→7 | 컴포넌트 오판 |
| 29 | task_4 | os_018 / network delay | 0.5 | 0.0 | 9→4 | 조기 제출 |
| 33 | task_6 | db_007 / db close | 1.0 | 0.0 | 6→6 | 컴포넌트 오판 (db_012) |
| 35 | task_6 | docker_001 / CPU fault | 0.5 | 0.0 | 6→5 | 컴포넌트 + 이유 오판 |

#### 개선: 원본 실패 → Critic 성공 (7건)

| idx | Task | 정답 | 원본 점수 | Critic 점수 |
|-----|------|------|----------|------------|
| 1 | task_4 | docker_004 / CPU fault | 0.0 | 0.50 |
| 15 | task_7 | os_007 / CPU fault | 0.0 | 0.33 |
| 21 | task_7 | docker_003 / CPU fault | 0.0 | 0.67 |
| 27 | task_7 | os_010 / CPU fault | 0.0 | 0.33 |
| 28 | task_7 | os_019 / network loss | 0.0 | 1.00 |
| 40 | task_2 | docker_005 / CPU fault | 0.0 | 1.00 |
| 49 | task_1 | docker_004 / CPU fault | 0.0 | 1.00 |

---

### Critic 실패 분석: Critic이 직접적으로 regression에 기여한 케이스

#### Case 1: idx=3 — Critic이 맞는 component를 의심

Agent가 docker_002를 최다 high-latency trace destination(2,824건)으로 정확히 식별했으나 reason을 "network delay"로 잘못 제출.

**Critic 피드백 (Step 36):**
```
biased_focus: docker_002를 단정하지만 docker_003, docker_001, docker_004가
거의 동일한 count를 가짐

wrong_interpretation: network delay라는 인과 메커니즘을 주장하지만
raw result에 근거 없음

missed_anomaly: docker_006, docker_005, docker_007, docker_008도
high-latency hop으로 나타남
```

→ Critic 피드백 후 instruction이 **docker_008**로 변경 — 정답(docker_002)에서 더 멀어짐.

**Critic이 했어야 할 말:** "component docker_002는 최다 trace count로 뒷받침됨. 그러나 'network delay'는 확인 안 됨 — container_cpu_used 메트릭을 확인하여 CPU fault 여부를 판별해야 함."

**원본 agent:** container_cpu_used에서 docker_002 CPU spike 확인 → "CPU fault" 제출 → **1.0점**

#### Case 2: idx=16 — Critic이 "no issues"로 오답 승인

Agent가 db_007을 정확히 찾았지만 trace elapsedTime만으로 "network delay" 결론.

**Critic 피드백 (Step 5):**
```
no issues
```

**Critic이 했어야 할 말:** "'network delay'가 적절히 뒷받침되지 않음. 높은 elapsedTime은 db close도 원인일 수 있음. db_007의 On_Off_State를 확인하여 network delay와 db close를 구분해야 함."

**원본 agent:** db_007 발견 후 On_Off_State 확인:
> "db_007의 On_Off_State가 0으로 떨어졌으므로 db close가 원인"

→ **1.0점**

#### Case 3: idx=33 — Critic이 논리적 모순에 침묵

Agent가 db_012에 대한 trace 검증 시도:
```
"Trace validation for db_012 during its metric fault window found
no supporting evidence in traces."
```

Trace가 0건인데도 db_012를 root cause로 제출. **Critic은 피드백을 제공하지 않음.**

**Critic이 했어야 할 말:** "db_012에 trace span이 0건이면 root cause가 될 수 없음. 다른 high-fault component(예: db_007)를 trace로 검증해야 함."

**원본 agent:** db_007에서 2,006개 span 전부 실패 + On_Off_State=0 확인 → **1.0점**

#### Case 4: idx=35 — Critic이 사소한 포맷 이슈만 지적

Agent가 container CPU 메트릭을 확인하지 않고 db_007 / "network delay" 결론 (정답: docker_001 / CPU fault).

**Critic 피드백:**
```
wrong_interpretation: payload에 '2020-05-26 21:00:09'가 있는데
UTC 표기가 없음. 'UTC'는 summary_text에만 있음.
```

컴포넌트 오류, 이유 오류라는 핵심 문제를 완전히 무시하고 **timestamp 포맷만 지적.**

**원본 agent:** container_cpu_used에서 docker_001 CPU spike 확인 → "CPU fault" 제출 → **0.5점**

---

### Critic 실패 근본 원인 분석

#### 1. 도메인 지식 부재

Critic 프롬프트에 RCA 도메인 규칙이 전혀 없음. 아래 판단을 할 수 없음:

| 지식 공백 | 예시 |
|----------|------|
| KPI→reason 매핑 | On_Off_State=0 → "db close" |
| reason별 필수 증거 | "CPU fault"는 container_cpu_used 또는 CPU_* KPI 필요 |
| reason 구분 기준 | 높은 elapsedTime만으로는 CPU fault / network delay / db close 구분 불가 |
| level 계층 | container-level fault는 container 메트릭으로 확인 필요 |

#### 2. Stateless — 분석 커버리지 추적 불가

매 Critic 호출이 독립적이므로 감지 불가:
- "Agent가 Step 3에서 CPU fault 발견했지만 Step 5에서 포기함" (idx=17)
- "Agent가 'no output' 루프에 10 step째 빠져있음" (idx=3, 24)
- "Agent가 Step 4에서 trace 분석 없이 submit 하려 함" (idx=11, 29)

#### 3. 검증 규칙이 표면적

현재 규칙은 **데이터↔텍스트 일치**만 확인:

| 현재 규칙 | 감지 가능 | 감지 불가 |
|----------|----------|----------|
| missed_anomaly | raw result의 숫자가 observation에 없음 | 수행되지 않은 분석 단계 |
| wrong_interpretation | observation이 raw data 수치와 모순 | 논리적으로 유효하지 않은 추론 |
| biased_focus | 한 component만 주목 | 잘못된 level에서의 분석 |

#### 4. 제출 게이트 부재

submit 직전에 특별 검증이 없음:
- reason이 필수 KPI로 확인되었나?
- component가 trace 증거로 뒷받침되는가?
- 대안이 배제되었는가?

---

### 개선 방안: Domain-Aware Critic (Stateless 유지)

Stateless를 유지하면서 도메인 지식을 매 호출 시 system prompt로 주입하는 방식.

#### Layer 1: Reason-Evidence Matrix (이유별 필수 증거)

```
root cause reason을 결론내기 전 반드시 확인해야 할 증거:

| reason              | 필수 확인 증거                              | 배제 필요                     |
|---------------------|---------------------------------------------|-------------------------------|
| CPU fault           | container_cpu_used spike 또는 CPU_* KPI     | -                             |
| network delay       | 높은 elapsedTime + On_Off_State ≠ 0         | db close (On_Off_State=0)     |
| db close            | On_Off_State = 0                            | -                             |
| network loss        | net_if_*_drop spike 또는 succee_rate 급락   | -                             |
| db connection limit | connected_clients 이상                      | db close (On_Off_State=0)     |
```

#### Layer 2: Logical Contradiction Rules (논리적 모순 규칙)

```
logical_contradiction으로 플래그:
- component가 fault window에서 trace span 0건 → root cause 불가
- reason이 "network delay"인데 container CPU 메트릭에 spike → CPU fault 가능성
- service level component인데 container-level fault가 더 높은 severity
- metric anomaly가 noise 임계값 미만 (breach_ratio ≤ 0.5)
```

#### Layer 3: Submission Readiness Gate (제출 준비도 검증)

```
observation에 submit 의도가 감지되면 추가 검증:
- component의 level(node/container/service)이 결정적 증거로 뒷받침되는가?
- reason이 필수 KPI로 확인되었는가? (Reason-Evidence Matrix 참조)
- 같은 level의 대안 component가 trace로 배제되었는가?
- occurrence datetime이 구체적 fault onset인가? (window 경계값이 아닌가?)
```

#### Layer 4: Analysis Quality Checks (분석 품질 검증)

```
insufficient_analysis로 플래그:
- "(Code executed successfully with no output)" 반복 → 코드 수정 필요
- metric과 trace를 교차 검증하지 않고 결론
- window 시작/종료 시간을 occurrence datetime으로 사용
- trace elapsedTime만으로 reason 결정 (메트릭 확인 없이)
```

이 규칙들은 매 Critic 호출 시 system prompt에 포함되므로, **Stateless를 유지하면서도 RCA 분석의 논리적 타당성을 검증**할 수 있음.

---

## 권장 사항

두 구현 모두 코드 레벨 agent 로직보다 **프롬프트 엔지니어링에 크게 의존하는 단순화된 버전**임. 개선을 위해:

1. **Action 파싱 추가** - 응답에서 Thought/Action 추출 및 검증
2. **Observation 포맷팅** - 환경 출력에 명확한 라벨링
3. **오류 감지 추가** - agent가 루프에 빠지거나 실수할 때 감지
4. **추론 체인 추적** - 결정 품질 저장 및 분석
5. **flash.py 버그 수정** - 누락된 return문, 오타
6. **ReAct 루프 정규화** - Parse → Execute → Observe → Repeat (적절한 상태 관리)
7. **Critic에 도메인 지식 추가** - RCA 특화 검증 규칙 (위 개선 방안 참조)