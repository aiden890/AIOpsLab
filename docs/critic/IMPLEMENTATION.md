# Critic Agent Implementation Plan

## Phase 1: Critic 프롬프트 작성

### 파일: `clients/openrca_rca/prompts/critic_prompt.py`

- Critic 시스템 프롬프트 정의
- 검증 규칙 (missed_anomaly, wrong_interpretation, biased_focus)
- JSON 출력 형식 정의

## Phase 2: ReactRCACriticAgent 구현

### 파일: `clients/openrca_rca/react_rca_critic_agent.py`

기존 `ReactRCAAgent`의 `get_action()` 메서드를 3단계로 분리:

### 2.1 `_generate_observation(feedback) → str`

- Controller에게 **observation만** 요청하는 LLM 호출
- 프롬프트에 "observation만 생성하라, action은 생성하지 마라" 지시
- 응답 형식: `{"observation": "..."}`

### 2.2 `_run_critic(raw_feedback, observation) → dict`

- Critic 시스템 프롬프트 + raw executor 결과 + Controller observation 입력
- **별도의 history** 관리 (Controller history에 포함하지 않음)
- 응답: `{"has_issues": bool, "issues": [...], "revised_observation": str|null}`
- `has_issues=false`면 원래 observation 유지

### 2.3 `_generate_instruction(observation) → str`

- (Critic이 수정한) observation을 Controller history에 추가
- Controller에게 action JSON 요청
- 기존 `get_action`의 action 파싱/빌드 로직 재사용
- 응답 형식: `{"thought": "...", "action": "...", "args": {...}}`

### 2.4 `get_action(feedback) → str` (통합)

```python
async def get_action(self, feedback: str) -> str:
    self.step += 1

    # 1. Observation
    observation = self._generate_observation(feedback)

    # 2. Critic
    critique = self._run_critic(raw_feedback=feedback, observation=observation)

    # 3. Instruction
    if critique["has_issues"]:
        final_obs = critique["revised_observation"]
        logger.info(f"Step[{self.step}] Critic revised observation")
    else:
        final_obs = observation

    return self._generate_instruction(final_obs)
```

## Phase 3: 실행 스크립트 연동

### 파일: `clients/react_static.py` (또는 별도 스크립트)

- `ReactRCACriticAgent`를 import하여 orchestrator에 등록
- 기존 `react_static.py`를 복사하여 `react_static_critic.py` 생성
- agent 클래스만 교체

```python
from clients.openrca_rca.react_rca_critic_agent import ReactRCACriticAgent

agent = ReactRCACriticAgent()
orchestrator.register_agent(agent, name="react-rca-critic")
```

## Phase 4: Logging & 분석

### Critic 로그 기록

각 step에서 Critic의 판단을 로그에 기록:

```
Step[3] Critic: has_issues=True
  - missed_anomaly: MG01 memory_usage 92.10% not mentioned
  - biased_focus: MG01 has higher deviation than Tomcat01
Step[3] Using revised observation
```

### 결과 비교를 위한 저장

- `results/` 디렉토리에 critic 개입 횟수, 이슈 유형별 통계 저장
- 기존 agent와의 정확도 비교에 활용

## Implementation Checklist

| # | Task | File |
|---|------|------|
| 1 | Critic 시스템 프롬프트 작성 | `prompts/critic_prompt.py` |
| 2 | ReactRCACriticAgent 클래스 구현 | `react_rca_critic_agent.py` |
| 3 | observation 전용 프롬프트 추가 | `prompts/controller_prompt.py` 또는 agent 내부 |
| 4 | 실행 스크립트 작성 | `react_static_critic.py` |
| 5 | 테스트 실행 (bank dataset, 1-2 문제) | - |
| 6 | 로그 확인 및 Critic 개입 효과 분석 | - |
