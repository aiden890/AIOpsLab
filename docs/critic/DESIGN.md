# Critic Agent Design

## 1. Problem

현재 ReactRCAAgent는 Controller가 하나의 LLM 호출로 **observation(thought)과 instruction(action)을 동시에 생성**한다.
이 구조에서 다음과 같은 추론 오류가 발생한다:

1. **이상치 무시**: Executor 결과에 여러 컴포넌트의 이상치가 포함되어 있지만, Controller가 일부만 주목하고 나머지를 무시
2. **잘못된 추론**: 데이터가 가리키는 방향과 다른 결론을 도출

이러한 오류가 다음 instruction에 그대로 반영되어 잘못된 방향으로 진단이 진행된다.

## 2. Solution: Critic Agent

Controller의 reasoning 단계를 **observation → critic → instruction** 3단계로 분리한다.

### 현재 흐름 (1-step)

```
Executor 결과 → Controller → { thought + action } → Environment
```

### 제안 흐름 (2-step with Critic)

```
Executor 결과 → Controller → { observation }
                                    ↓
                              Critic → { critique }
                                    ↓
                    Controller → { instruction (action) }
```

## 3. Architecture

### 3.1 전체 루프

```
┌─────────────────────────────────────────────────────────────┐
│  for step in range(max_steps):                              │
│                                                             │
│    1. Controller LLM Call #1: Observation 생성              │
│       Input:  history + feedback                            │
│       Output: { "observation": "..." }                      │
│                                                             │
│    2. Critic LLM Call: Observation 검증                     │
│       Input:  executor_result(raw) + observation            │
│       Output: { "issues": [...], "revised_observation": "..." } │
│                                                             │
│    3. Controller LLM Call #2: Instruction 생성              │
│       Input:  history + critique result                     │
│       Output: { "instruction": "...", "action": "...", "args": {...} } │
│                                                             │
│    4. Environment 실행 → feedback                           │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 Critic의 역할

Critic은 **Controller의 observation과 raw executor 결과를 비교**하여:

| 검증 항목 | 설명 |
|-----------|------|
| 누락된 이상치 | executor 결과에 있지만 observation에서 언급하지 않은 컴포넌트/KPI |
| 잘못된 해석 | 데이터 값과 다른 해석 (e.g., 높은 값을 낮다고 판단) |
| 편향된 결론 | 한 컴포넌트에만 집중하고 다른 후보를 배제 |

### 3.3 Critic 출력 형식

```json
{
    "has_issues": true,
    "issues": [
        {
            "type": "missed_anomaly",
            "detail": "MG01의 memory_usage(92.1%)가 threshold(80%)를 초과했으나 observation에서 누락됨"
        }
    ],
    "revised_observation": "Tomcat01(CPU 85.2%)과 MG01(memory 92.1%) 모두 threshold 초과. 두 컴포넌트 모두 조사 필요."
}
```

### 3.4 Critic이 문제 없다고 판단한 경우

```json
{
    "has_issues": false,
    "issues": [],
    "revised_observation": null
}
```

이 경우 원래 observation을 그대로 사용하여 instruction을 생성한다.

### 3.5 Controller 응답 형식 변경

**Step 1 - Observation 전용 응답:**
```json
{
    "observation": "Executor 결과 분석: Tomcat01의 CPU가 85.2%로 threshold 초과..."
}
```

**Step 2 - Instruction 응답 (기존과 동일):**
```json
{
    "instruction": "<critic 반영한 최종 분석>",
    "action": "<action_name>",
    "args": {"<param>": "<value>"}
}
```

## 4. Component Design

### 4.1 새로운 클래스

```
clients/openrca_rca/
├── react_rca_agent.py          # 기존 (수정 없음, 유지)
├── react_rca_critic_agent.py   # 신규: Critic이 포함된 agent
└── prompts/
    └── critic_prompt.py        # 신규: Critic 시스템 프롬프트
```

### 4.2 ReactRCACriticAgent

`ReactRCAAgent`를 상속하거나 별도 클래스로 구현. 핵심 변경:

```python
class ReactRCACriticAgent:
    """ReactRCAAgent with Critic validation between observation and instruction."""

    def __init__(self, api_config_path=None):
        self.configs = load_config(api_config_path)
        self.history: list[dict] = []
        self.critic_history: list[dict] = []  # Critic 전용 context
        self.step = 0

    async def get_action(self, feedback: str) -> str:
        # Phase 1: Controller generates observation
        observation = self._generate_observation(feedback)

        # Phase 2: Critic validates observation against raw feedback
        critique = self._run_critic(raw_feedback=feedback, observation=observation)

        # Phase 3: Controller generates instruction using (possibly revised) observation
        final_observation = critique["revised_observation"] or observation
        return self._generate_instruction(final_observation)
```

### 4.3 Critic Prompt 핵심 구조

```python
CRITIC_SYSTEM_PROMPT = """
You are a Critic agent for root cause analysis.

Your role:
- Compare the Controller's observation against the raw executor result
- Identify any anomalies, components, or patterns that the Controller missed or misinterpreted
- You do NOT generate instructions or actions — only validate observations

Rules:
1. If the executor result contains components with values exceeding thresholds
   that the observation does not mention → flag as "missed_anomaly"
2. If the observation draws a conclusion that contradicts the data → flag as "wrong_interpretation"
3. If the observation focuses on only one component while others show equal or higher
   severity → flag as "biased_focus"
4. If no issues found, set has_issues=false

Respond ONLY with JSON:
{
    "has_issues": true/false,
    "issues": [...],
    "revised_observation": "..." or null
}
"""
```

### 4.4 LLM 호출 구성

| 호출 | LLM | reasoning_effort | 용도 |
|------|-----|-----------------|------|
| Controller #1 (observation) | GPT-5 | low | 빠른 관찰 요약 |
| Critic | GPT-5 | low | 검증 (입출력 비교) |
| Controller #2 (instruction) | GPT-5 | low | 다음 행동 결정 |

> 참고: Critic은 reasoning이 아닌 **비교 검증** 작업이므로 reasoning_effort=low로 충분.
> 필요 시 별도 모델 또는 effort 수준을 설정할 수 있도록 config에서 분리.

## 5. Data Flow Example

### Input: KPI deviation 조회 결과

```
HIGH KPI deviations (above P90) for 'static-bank':
   component       kpi_name           peak_value
0  Tomcat01       CPU_CPUCpuUtil         85.23
1  MG01           memory_usage           92.10
2  Redis01        connections            45.20
```

### Controller Observation (문제 있는 경우)

```json
{
    "observation": "Tomcat01의 CPU가 85.23%로 비정상. 이 컴포넌트를 집중 조사해야 함."
}
```

### Critic Output

```json
{
    "has_issues": true,
    "issues": [
        {"type": "missed_anomaly", "detail": "MG01 memory_usage 92.10% — observation에서 누락"},
        {"type": "biased_focus", "detail": "MG01이 Tomcat01보다 높은 deviation을 보이지만 무시됨"}
    ],
    "revised_observation": "Tomcat01(CPU 85.23%)과 MG01(memory 92.10%) 모두 threshold 초과. MG01이 더 높은 deviation. Redis01 connections(45.20)도 확인 필요."
}
```

### Controller Instruction (Critic 반영 후)

```json
{
    "thought": "MG01과 Tomcat01 모두 이상. MG01이 더 심각. trace로 두 컴포넌트 관계 확인 필요.",
    "action": "get_trace_call_graph",
    "args": {"namespace": "static-bank", "faulty_components": ["MG01", "Tomcat01"]}
}
```
