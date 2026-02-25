# Telemetry Ablation - Group 2: Market

**총 tasks: 1,936** (Market CB1 234×4 + CB2 250×4)
**병렬 프로세스: Phase별 4개 (CB1 → CB2 순차)**

> Group 1 (Bank + Telecom, 2,006 tasks)는 [EXPERIMENT_GROUP1.md](EXPERIMENT_GROUP1.md) 참고.

---

## 실험 개요

텔레메트리 타입(log, metric, trace)을 하나씩 제거했을 때 RCA 정확도에 미치는 영향을 측정하는 ablation 실험.

### 아키텍처

- **RCA Agent** = Controller(LLM) + Executor(LLM + IPython)
- Controller가 분석 단계를 계획 → Executor가 Python 코드 생성·실행 → 결과 반환
- 텔레메트리는 IPython에 주입된 `TelemetryHelper`로 접근:
  - `telemetry.get_logs()` → CSV file path (or None)
  - `telemetry.get_metrics()` → CSV file path (or None)
  - `telemetry.get_traces()` → CSV file path (or None)

### Ablation 메커니즘 (2 레벨)

1. **Docker 컨테이너**: 비활성 텔레메트리는 데이터 자체를 로드하지 않음
2. **프롬프트**: `build_schema(condition)`으로 비활성 타입을 schema/guide에서 제거 + 제한 안내 추가

각 condition별 독립 Docker 컨테이너 생성 (e.g., `static-dataset-static-market-cb1-no-log`).

### Per-problem 스킵 로직

`run_rca_agent.py`는 results-dir에 해당 문제의 JSON이 이미 존재하면 자동 스킵.
→ **중단 후 재시작해도 완료된 문제는 건너뜀** (안전하게 재실행 가능).

### CB1/CB2 순차 실행 규칙

Market 데이터셋은 CB1(cloudbed-1, 234문제)과 CB2(cloudbed-2, 250문제)로 나뉨.
**같은 condition의 CB1과 CB2는 같은 results-dir을 공유**하므로 동시 실행하면 JSON 파일이 충돌할 수 있음.
→ Phase A(CB1 4개) 완료 후 Phase B(CB2 4개) 실행.

---

## 실험 목록

| # | Eval ID | Dataset | Condition | Problems (CB1+CB2) |
|---|---------|---------|-----------|---------------------|
| 1 | market_all | openrca_market_cb1 + cb2 | all | 234 + 250 = 484 |
| 2 | market_no_log | openrca_market_cb1 + cb2 | no_log | 234 + 250 = 484 |
| 3 | market_no_metric | openrca_market_cb1 + cb2 | no_metric | 234 + 250 = 484 |
| 4 | market_no_trace | openrca_market_cb1 + cb2 | no_trace | 234 + 250 = 484 |

---

## Step 1: 사전 조건 확인

```bash
# Docker 실행 확인
docker ps > /dev/null 2>&1 && echo "OK: Docker running" || echo "FAIL: Docker not running"

# API 설정 확인
test -f clients/openrca_rca/api_config.yaml && echo "OK: API config exists" || echo "FAIL: api_config.yaml missing"

# 기존 컨테이너 충돌 확인 (있으면 정리)
docker ps --filter "name=static-dataset" --format "{{.Names}}"
docker ps --filter "name=static-dataset" -q | xargs -r docker stop
docker ps -a --filter "name=static-dataset" -q | xargs -r docker rm
```

하나라도 FAIL이면 사용자에게 알리고 중단.

## Step 2: 현재 진행 상황 확인

```bash
echo "=== Group 2 Results ==="
for d in market_all market_no_log market_no_metric market_no_trace; do
  dir="experiments/telemetry_ablation/results/$d"
  count=$(find "$dir" -name "*.json" -not -name "metadata.json" 2>/dev/null | wc -l)
  echo "  $d: $count"
done
```

## Step 3: Phase A 실행 — Market CB1 (4 프로세스)

모든 프로세스를 백그라운드로 실행합니다. 반드시 Bash의 `run_in_background` 옵션을 사용하세요.

```bash
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition all       --work-dir /tmp/ablation_mcb1_all       --results-dir experiments/telemetry_ablation/results/market_all &
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_log    --work-dir /tmp/ablation_mcb1_no_log    --results-dir experiments/telemetry_ablation/results/market_no_log &
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_metric --work-dir /tmp/ablation_mcb1_no_metric --results-dir experiments/telemetry_ablation/results/market_no_metric &
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_trace  --work-dir /tmp/ablation_mcb1_no_trace  --results-dir experiments/telemetry_ablation/results/market_no_trace &
```

## Step 4: 모니터링 (10분 간격)

프로세스가 실행되는 동안 10분마다 아래를 실행하세요.

```bash
# 1. 실행 중인 프로세스 수
echo "=== Running processes ==="
ps aux | grep run_rca_agent | grep -v grep | wc -l

# 2. 조건별 완료 현황
echo "=== Group 2 Results ==="
for d in market_all market_no_log market_no_metric market_no_trace; do
  dir="experiments/telemetry_ablation/results/$d"
  count=$(find "$dir" -name "*.json" -not -name "metadata.json" 2>/dev/null | wc -l)
  echo "  $d: $count"
done

# 3. Docker 컨테이너 상태 (모두 Up이어야 정상)
echo "=== Containers ==="
docker ps --filter "name=static-dataset" --format "{{.Names}}: {{.Status}}"
```

**이상 징후 대응:**
- 프로세스 수가 줄었는데 결과가 덜 나옴 → 해당 조건 재실행 (Step 6)
- 컨테이너가 Exited → 해당 프로세스 죽었을 가능성, 재실행
- 결과 수가 10분간 변동 없음 → rate limit 대기 중이거나 행(hang). `ps aux`로 CPU 확인

## Step 5: Phase A 완료 확인 후 Phase B 실행

```bash
# Phase A 프로세스 종료 확인 (0이면 Phase B 시작)
ps aux | grep run_rca_agent | grep -v grep | wc -l
```

**Phase B: Market CB2 (4 프로세스)**

```bash
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition all       --work-dir /tmp/ablation_mcb2_all       --results-dir experiments/telemetry_ablation/results/market_all &
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_log    --work-dir /tmp/ablation_mcb2_no_log    --results-dir experiments/telemetry_ablation/results/market_no_log &
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_metric --work-dir /tmp/ablation_mcb2_no_metric --results-dir experiments/telemetry_ablation/results/market_no_metric &
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_trace  --work-dir /tmp/ablation_mcb2_no_trace  --results-dir experiments/telemetry_ablation/results/market_no_trace &
```

Phase B에서도 Step 4 모니터링을 계속합니다.

## Step 6: 에러 복구

실패한 프로세스는 같은 명령어를 다시 실행하면 됩니다. 이미 완료된 문제는 자동 스킵됩니다.

```bash
# 예: market_no_metric CB1 프로세스가 죽었을 때
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_metric \
  --work-dir /tmp/ablation_mcb1_no_metric \
  --results-dir experiments/telemetry_ablation/results/market_no_metric &

# 예: market_no_metric CB2 프로세스가 죽었을 때
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_metric \
  --work-dir /tmp/ablation_mcb2_no_metric \
  --results-dir experiments/telemetry_ablation/results/market_no_metric &
```

## Step 7: 완료 확인

```bash
# 프로세스 종료 확인 (0이면 완료)
ps aux | grep run_rca_agent | grep -v grep | wc -l

# 결과 요약
python experiments/telemetry_ablation/run_ablation.py --summary-only
```

### 기대 결과 수

| Eval ID | CB1 | CB2 | 합계 |
|---------|-----|-----|------|
| market_all | 234 | 250 | 484 |
| market_no_log | 234 | 250 | 484 |
| market_no_metric | 234 | 250 | 484 |
| market_no_trace | 234 | 250 | 484 |
| **합계** | **936** | **1,000** | **1,936** |
