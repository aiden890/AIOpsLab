# Experiment Commands

## Original vs Critic 비교 실험

### Original (openrca-rca)

Controller + Executor 2-agent 구조. Critic 없음.

```bash
python clients/run_rca_agent.py \
  --dataset openrca_telecom \
  --api-config clients/openrca_rca/api_config_xhigh.yaml \
  --eval-id gpt5-xhigh-original \
  --results-dir results/experiments/openrca_telecom
```

### Critic (react-rca-critic)

ReAct + Critic validation 구조. 각 observation 후 Critic LLM이 검증.

```bash
python clients/run_react_rca_critic.py \
  --dataset openrca_telecom \
  --api-config clients/openrca_rca/api_config_xhigh.yaml \
  --eval-id gpt5-xhigh-critic \
  --results-dir results/experiments/openrca_telecom
```

### 두 버전 동시 실행 (백그라운드)

```bash
mkdir -p logs

python clients/run_rca_agent.py \
  --dataset openrca_telecom \
  --api-config clients/openrca_rca/api_config_xhigh.yaml \
  --eval-id gpt5-xhigh-original \
  --results-dir results/experiments/openrca_telecom \
  > logs/original.log 2>&1 &

python clients/run_react_rca_critic.py \
  --dataset openrca_telecom \
  --api-config clients/openrca_rca/api_config_xhigh.yaml \
  --eval-id gpt5-xhigh-critic \
  --results-dir results/experiments/openrca_telecom \
  > logs/critic.log 2>&1 &
```

로그 실시간 확인:
```bash
tail -f logs/original.log
tail -f logs/critic.log
```

---

## gpt-5 low reasoning 실험

### Original (openrca-rca) — low

```bash
python clients/run_rca_agent.py \
  --dataset openrca_telecom \
  --api-config clients/openrca_rca/api_config_low.yaml \
  --eval-id gpt5-low-original \
  --results-dir results/experiments/openrca_telecom
```

### Critic (react-rca-critic) — low

```bash
python clients/run_react_rca_critic.py \
  --dataset openrca_telecom \
  --api-config clients/openrca_rca/api_config_low.yaml \
  --eval-id gpt5-low-critic \
  --results-dir results/experiments/openrca_telecom
```

### 두 버전 동시 실행 (백그라운드)

```bash
mkdir -p logs

python clients/run_rca_agent.py \
  --dataset openrca_telecom \
  --api-config clients/openrca_rca/api_config_low.yaml \
  --eval-id gpt5-low-original \
  --results-dir results/experiments/openrca_telecom \
  > logs/low-original.log 2>&1 &

python clients/run_react_rca_critic.py \
  --dataset openrca_telecom \
  --api-config clients/openrca_rca/api_config_low.yaml \
  --eval-id gpt5-low-critic \
  --results-dir results/experiments/openrca_telecom \
  > logs/low-critic.log 2>&1 &
```

로그 실시간 확인:
```bash
tail -f logs/low-original.log
tail -f logs/low-critic.log
```

---

## 공통 옵션

| 옵션 | 설명 | 기본값 |
|------|------|--------|
| `--dataset` | 실행할 데이터셋 | - |
| `--problem` | 단일 problem ID 실행 (예: `openrca_telecom-task_7-0`) | - |
| `--task-type` | 특정 task 유형만 실행 (예: `task_7`) | - |
| `--api-config` | LLM 설정 파일 경로 | `api_config.yaml` |
| `--eval-id` | 실험 식별자 (결과 폴더명) | 자동 UUID |
| `--results-dir` | 결과 저장 디렉토리 | `results/static_problems` |
| `--max-steps` | 최대 orchestrator 스텝 수 | 25 (original) / 40 (critic) |
| `--start-index` | N번째 problem부터 시작 (재시작 시 사용) | 0 |
| `--condition` | telemetry ablation 조건 (`all`, `no_log`, `no_metric`, `no_trace`) | `all` |

---

## api_config 파일 목록

| 파일 | 모델 | reasoning_effort |
|------|------|-----------------|
| `clients/openrca_rca/api_config.yaml` | gpt-5 | - |
| `clients/openrca_rca/api_config_low.yaml` | gpt-5 | low |
| `clients/openrca_rca/api_config_xhigh.yaml` | gpt-5 | xhigh |

---

## 결과 저장 구조

```
results/experiments/
  openrca_telecom/
    openrca-rca/          # original
      gpt-5-xhigh/
        gpt5-xhigh-original/
          scores.csv
          20260305_1530_task_7-0.json
          20260305_1530_task_7-0.log
    react-rca-critic/     # critic
      gpt-5-xhigh/
        gpt5-xhigh-critic/
          scores.csv
          20260305_1530_task_7-0.json
          20260305_1530_task_7-0.log
          task_7-0_executor.ipynb
```

---

## W&B

- 프로젝트: `aiopslab-rca`
- Run 이름: `{agent}/{model}/{dataset}/{eval-id}`
- 실험 단위로 1개 run에 전체 51개 task 통합
- W&B 활성화: `.env` 파일에 `USE_WANDB=true` 설정
