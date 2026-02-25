# Telemetry Ablation - Group 1: Bank + Telecom

**총 tasks: 2,006** (Bank 398×4 + Telecom 138×3)
**병렬 프로세스: 7개**

> Group 2 (Market, 1,936 tasks)는 [EXPERIMENT_GROUP2.md](EXPERIMENT_GROUP2.md) 참고.

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

각 condition별 독립 Docker 컨테이너 생성 (e.g., `static-dataset-static-bank-no-log`).

### Per-problem 스킵 로직

`run_rca_agent.py`는 results-dir에 해당 문제의 JSON이 이미 존재하면 자동 스킵.
→ **중단 후 재시작해도 완료된 문제는 건너뜀** (안전하게 재실행 가능).

---

## 실험 목록

| # | Eval ID | Dataset | Condition | Problems |
|---|---------|---------|-----------|----------|
| 1 | bank_all | openrca_bank | all | 398 |
| 2 | bank_no_log | openrca_bank | no_log | 398 |
| 3 | bank_no_metric | openrca_bank | no_metric | 398 |
| 4 | bank_no_trace | openrca_bank | no_trace | 398 |
| 5 | telecom_all | openrca_telecom | all | 138 |
| 6 | telecom_no_metric | openrca_telecom | no_metric | 138 |
| 7 | telecom_no_trace | openrca_telecom | no_trace | 138 |

> `telecom_no_log`은 실행하지 않음 (Telecom에 log 데이터가 없어 `all`과 동일)

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
echo "=== Group 1 Results ==="
for d in bank_all bank_no_log bank_no_metric bank_no_trace telecom_all telecom_no_metric telecom_no_trace; do
  dir="experiments/telemetry_ablation/results/$d"
  count=$(find "$dir" -name "*.json" -not -name "metadata.json" 2>/dev/null | wc -l)
  echo "  $d: $count"
done
```

## Step 3: 실행 (7 프로세스 동시)

모든 프로세스를 백그라운드로 실행합니다. 반드시 Bash의 `run_in_background` 옵션을 사용하세요.

```bash
# Bank (4 conditions)
python clients/run_rca_agent.py --dataset openrca_bank --condition all       --work-dir /tmp/ablation_bank_all       --results-dir experiments/telemetry_ablation/results/bank_all &
python clients/run_rca_agent.py --dataset openrca_bank --condition no_log    --work-dir /tmp/ablation_bank_no_log    --results-dir experiments/telemetry_ablation/results/bank_no_log &
python clients/run_rca_agent.py --dataset openrca_bank --condition no_metric --work-dir /tmp/ablation_bank_no_metric --results-dir experiments/telemetry_ablation/results/bank_no_metric &
python clients/run_rca_agent.py --dataset openrca_bank --condition no_trace  --work-dir /tmp/ablation_bank_no_trace  --results-dir experiments/telemetry_ablation/results/bank_no_trace &

# Telecom (3 conditions — no_log 제외)
python clients/run_rca_agent.py --dataset openrca_telecom --condition all       --work-dir /tmp/ablation_telecom_all       --results-dir experiments/telemetry_ablation/results/telecom_all &
python clients/run_rca_agent.py --dataset openrca_telecom --condition no_metric --work-dir /tmp/ablation_telecom_no_metric --results-dir experiments/telemetry_ablation/results/telecom_no_metric &
python clients/run_rca_agent.py --dataset openrca_telecom --condition no_trace  --work-dir /tmp/ablation_telecom_no_trace  --results-dir experiments/telemetry_ablation/results/telecom_no_trace &
```

## Step 4: 모니터링 (10분 간격)

프로세스가 실행되는 동안 10분마다 아래를 실행하세요.

```bash
# 1. 실행 중인 프로세스 수
echo "=== Running processes ==="
ps aux | grep run_rca_agent | grep -v grep | wc -l

# 2. 조건별 완료 현황
echo "=== Group 1 Results ==="
for d in bank_all bank_no_log bank_no_metric bank_no_trace telecom_all telecom_no_metric telecom_no_trace; do
  dir="experiments/telemetry_ablation/results/$d"
  count=$(find "$dir" -name "*.json" -not -name "metadata.json" 2>/dev/null | wc -l)
  echo "  $d: $count"
done

# 3. Docker 컨테이너 상태 (모두 Up이어야 정상)
echo "=== Containers ==="
docker ps --filter "name=static-dataset" --format "{{.Names}}: {{.Status}}"
```

**이상 징후 대응:**
- 프로세스 수가 줄었는데 결과가 덜 나옴 → 해당 조건 재실행 (Step 5)
- 컨테이너가 Exited → 해당 프로세스 죽었을 가능성, 재실행
- 결과 수가 10분간 변동 없음 → rate limit 대기 중이거나 행(hang). `ps aux`로 CPU 확인

## Step 5: 에러 복구

실패한 프로세스는 같은 명령어를 다시 실행하면 됩니다. 이미 완료된 문제는 자동 스킵됩니다.

```bash
# 예: bank_no_metric 프로세스가 죽었을 때
python clients/run_rca_agent.py --dataset openrca_bank --condition no_metric \
  --work-dir /tmp/ablation_bank_no_metric \
  --results-dir experiments/telemetry_ablation/results/bank_no_metric &
```

## Step 6: 완료 확인

```bash
# 프로세스 종료 확인 (0이면 완료)
ps aux | grep run_rca_agent | grep -v grep | wc -l

# 결과 요약
python experiments/telemetry_ablation/run_ablation.py --summary-only
```

### 기대 결과 수

| Eval ID | 기대 JSON 수 |
|---------|-------------|
| bank_all | 398 |
| bank_no_log | 398 |
| bank_no_metric | 398 |
| bank_no_trace | 398 |
| telecom_all | 138 |
| telecom_no_metric | 138 |
| telecom_no_trace | 138 |
| **합계** | **2,006** |
