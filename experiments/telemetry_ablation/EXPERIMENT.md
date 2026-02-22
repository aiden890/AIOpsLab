# Telemetry Ablation Experiment

## Objective

Measure how removing individual telemetry types (logs, metrics, traces) from
the **agent's prompt** affects RCA accuracy on OpenRCA static datasets.

The underlying data availability in the Docker container is also controlled —
disabled telemetry types are not loaded into the container.

## Agent Architecture

**RCA Agent** = Controller-Executor (`clients/run_rca_agent.py`):

1. **Controller** (LLM): Analyzes results step-by-step, produces atomic instructions
2. **Executor** (LLM + IPython): Translates instructions → Python code → executes in
   IPython kernel → returns LLM summary of output
3. Telemetry accessed via `TelemetryHelper` injected into IPython as `telemetry`:
   - `telemetry.get_logs()` → CSV file path
   - `telemetry.get_metrics()` → CSV file path
   - `telemetry.get_traces()` → CSV file path

### Key files

| File | Role |
|------|------|
| `clients/run_rca_agent.py` | CLI runner (entry point) |
| `clients/openrca_rca/agent.py` | Controller-Executor agent |
| `clients/openrca_rca/telemetry_helper.py` | `TelemetryHelper` (IPython bridge) |
| `clients/openrca_rca/prompts/telemetry_guide.py` | `build_telemetry_guide(condition)` |
| `clients/openrca_rca/prompts/basic_prompt_bank.py` | Bank: `build_schema(condition)`, `cand` |
| `clients/openrca_rca/prompts/basic_prompt_telecom.py` | Telecom: `build_schema(condition)`, `cand` |
| `clients/openrca_rca/prompts/basic_prompt_market.py` | Market: `build_schema(condition)`, `cand` |
| `clients/openrca_rca/prompts/__init__.py` | `get_basic_prompt(dataset_key)` router |
| `experiments/telemetry_ablation/run_ablation.py` | Batch experiment runner |

## Ablation Mechanism

### Two levels of ablation

1. **Docker container level** (`aiopslab/service/apps/static_dataset/dataset.py`):
   - `CONDITION_FLAGS` in `StaticDataset.__init__()` overrides telemetry config
   - Disabled types are NOT loaded into the container (no data files created)
   - Each condition gets its own Docker container via namespace suffixing

2. **Prompt level** (agent prompts):
   - `build_telemetry_guide(condition)`: removes disabled types from task description
   - `build_schema(condition)`: removes disabled types from controller system prompt
   - Adds explicit restriction notices for disabled types

### Container isolation

Each `--condition` creates a unique Docker container:

| Condition | Namespace | Container Name |
|-----------|-----------|----------------|
| `all` | `static-bank` | `static-dataset-static-bank` |
| `no_log` | `static-bank-no-log` | `static-dataset-static-bank-no-log` |
| `no_metric` | `static-bank-no-metric` | `static-dataset-static-bank-no-metric` |
| `no_trace` | `static-bank-no-trace` | `static-dataset-static-bank-no-trace` |

This allows **parallel execution** of different conditions without container conflicts.
Docker compose also uses `--project-name {namespace}` so compose_down only stops
the correct container.

### What changes per condition

| Prompt section | `all` | `no_log` | `no_metric` | `no_trace` |
|----------------|-------|----------|-------------|------------|
| Container: log data | loaded | **excluded** | loaded | loaded |
| Container: metric data | loaded | loaded | **excluded** | loaded |
| Container: trace data | loaded | loaded | loaded | **excluded** |
| Telemetry guide: `get_logs()` | shown | **hidden** | shown | shown |
| Telemetry guide: `get_metrics()` | shown | shown | **hidden** | shown |
| Telemetry guide: `get_traces()` | shown | shown | shown | **hidden** |
| Restriction notice | none | "Log data is NOT available..." | "Metric data is NOT available..." | "Trace data is NOT available..." |
| Schema: log columns | shown | **hidden** | shown | shown |
| Schema: metric columns | shown | shown | **hidden** | shown |
| Schema: trace columns | shown | shown | shown | **hidden** |

## Experiment Matrix

### Datasets and available telemetry

| Short name | Config file(s) | Dataset key(s) | ~Problems | Available telemetry |
|------------|---------------|----------------|-----------|---------------------|
| bank | `openrca_bank.json` | `openrca_bank` | ~120 | log + metric + trace |
| telecom | `openrca_telecom.json` | `openrca_telecom` | ~29 | **metric + trace only** (NO logs) |
| market | `openrca_market_cloudbed1.json`, `openrca_market_cloudbed2.json` | `openrca_market_cb1`, `openrca_market_cb2` | ~180 | log + metric + trace |

Config directory: `aiopslab/service/apps/static_dataset/config/`

### Conditions

| Condition | enable_log | enable_metric | enable_trace |
|-----------|-----------|--------------|-------------|
| all | true | true | true |
| no_log | false | true | true |
| no_metric | true | false | true |
| no_trace | true | true | false |

### Effective matrix: 11 experiments (not 12)

Since **Telecom has no log data**, `telecom_no_log` is identical to `telecom_all`
and is automatically skipped.

| Dataset | all | no_log | no_metric | no_trace |
|---------|-----|--------|-----------|----------|
| bank | run | run | run | run |
| telecom | run | **SKIP** (= all) | run | run |
| market | run | run | run | run |

**Total: 11 effective experiments** (~330 problems × 3.3 conditions avg ≈ ~1,100 runs)

## Running experiments

### Quick test (single problem, few steps)

```bash
python clients/run_rca_agent.py \
  --problem openrca_bank-task_1-0 \
  --condition no_trace \
  --max-steps 3 \
  --results-dir results_test \
  --work-dir /tmp/test_ablation
```

### Batch runner (recommended)

```bash
# Run all 11 experiments (auto-skips telecom_no_log and completed ones)
python experiments/telemetry_ablation/run_ablation.py

# Single dataset
python experiments/telemetry_ablation/run_ablation.py --dataset bank

# Single experiment
python experiments/telemetry_ablation/run_ablation.py --dataset bank --condition all

# Force re-run completed
python experiments/telemetry_ablation/run_ablation.py --force

# Check progress only
python experiments/telemetry_ablation/run_ablation.py --summary-only
```

### Manual parallel execution (all datasets simultaneously)

Use `--work-dir` to isolate telemetry CSV files per run. Each run saves CSVs to
`{work_dir}/static_*_output/`, so concurrent runs without unique work-dirs
would overwrite each other's files.

```bash
# Bank (4 conditions)
python clients/run_rca_agent.py --dataset openrca_bank --condition all       --work-dir /tmp/ablation_bank_all       --results-dir experiments/telemetry_ablation/results/bank_all &
python clients/run_rca_agent.py --dataset openrca_bank --condition no_log    --work-dir /tmp/ablation_bank_no_log    --results-dir experiments/telemetry_ablation/results/bank_no_log &
python clients/run_rca_agent.py --dataset openrca_bank --condition no_metric --work-dir /tmp/ablation_bank_no_metric --results-dir experiments/telemetry_ablation/results/bank_no_metric &
python clients/run_rca_agent.py --dataset openrca_bank --condition no_trace  --work-dir /tmp/ablation_bank_no_trace  --results-dir experiments/telemetry_ablation/results/bank_no_trace &

# Telecom (3 conditions — no_log skipped, same as all)
python clients/run_rca_agent.py --dataset openrca_telecom --condition all       --work-dir /tmp/ablation_telecom_all       --results-dir experiments/telemetry_ablation/results/telecom_all &
python clients/run_rca_agent.py --dataset openrca_telecom --condition no_metric --work-dir /tmp/ablation_telecom_no_metric --results-dir experiments/telemetry_ablation/results/telecom_no_metric &
python clients/run_rca_agent.py --dataset openrca_telecom --condition no_trace  --work-dir /tmp/ablation_telecom_no_trace  --results-dir experiments/telemetry_ablation/results/telecom_no_trace &

# Market CB1 (4 conditions)
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition all       --work-dir /tmp/ablation_market_cb1_all       --results-dir experiments/telemetry_ablation/results/market_all &
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_log    --work-dir /tmp/ablation_market_cb1_no_log    --results-dir experiments/telemetry_ablation/results/market_no_log &
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_metric --work-dir /tmp/ablation_market_cb1_no_metric --results-dir experiments/telemetry_ablation/results/market_no_metric &
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_trace  --work-dir /tmp/ablation_market_cb1_no_trace  --results-dir experiments/telemetry_ablation/results/market_no_trace &

# Market CB2 (run after CB1 or use separate results-dir for parallel)
# CB1 and CB2 share the same results-dir per condition, so run sequentially:
wait  # wait for all background jobs
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition all       --work-dir /tmp/ablation_market_cb2_all       --results-dir experiments/telemetry_ablation/results/market_all &
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_log    --work-dir /tmp/ablation_market_cb2_no_log    --results-dir experiments/telemetry_ablation/results/market_no_log &
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_metric --work-dir /tmp/ablation_market_cb2_no_metric --results-dir experiments/telemetry_ablation/results/market_no_metric &
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_trace  --work-dir /tmp/ablation_market_cb2_no_trace  --results-dir experiments/telemetry_ablation/results/market_no_trace &
```

### Per-problem skip logic

`run_rca_agent.py` automatically skips problems that already have result JSON files
in the results directory. This means:
- Interrupted runs can be safely resumed
- Existing valid results are preserved on re-run
- Use different `--results-dir` to force a clean run

## Result format

### Per-problem JSON (`results/{eval_id}/openrca-rca/{model}_{task}_{timestamp}.json`)

```json
{
    "agent": "openrca-rca",
    "session_id": "...",
    "problem_id": "openrca_bank-task_1-0",
    "condition": "no_trace",
    "start_time": 1771769986.37,
    "end_time": 1771770044.88,
    "trace": [
        {"role": "assistant", "content": "execute(\"...\")"},
        {"role": "env", "content": "...executor result..."},
        ...
    ],
    "executor_trajectory": [
        {"step": 1, "instruction": "...", "code": "...", "result": "...", "success": true},
        ...
    ],
    "results": {
        "score": 0.5,
        "success": false,
        "task_type": "task_1",
        "difficulty": "...",
        "steps": 6,
        "TTA": 58.5,
        "passing_criteria": "...",
        "failing_criteria": "..."
    }
}
```

Key fields:
- `condition`: ablation condition (`"all"`, `"no_log"`, `"no_metric"`, `"no_trace"`)
- `trace`: Controller <-> Environment conversation history
- `executor_trajectory`: per-step Executor details (instruction, generated code, result)
- `results.score`: 0.0-1.0 evaluation score

### Batch results (`experiments/telemetry_ablation/results/`)

```
experiments/telemetry_ablation/results/
├── RESULTS.md                 (auto-generated summary table)
├── bank_all/
│   ├── metadata.json          (experiment config)
│   └── openrca_bank/openrca-rca/*.json
├── bank_no_log/...
├── telecom_all/...
└── market_all/...             (contains both cb1 and cb2)
```

### Summary table (`RESULTS.md`)

Auto-generated by `run_ablation.py --summary-only`:

```
| Dataset | all    | no_log | no_metric | no_trace |
|---------|--------|--------|-----------|----------|
| bank    | 0.5000 | 0.4500 | 0.3000    | 0.4800   |
| telecom | 0.3500 | N/A    | 0.2000    | 0.3200   |
| market  | -      | -      | -         | -        |
```

## CLI reference

### `clients/run_rca_agent.py`

| Flag | Default | Description |
|------|---------|-------------|
| `--problem` | - | Single problem ID (e.g., `openrca_bank-task_1-0`) |
| `--dataset` | - | Run all problems in dataset (e.g., `openrca_bank`) |
| `--task-type` | - | Filter by task type (e.g., `task_1`) |
| `--condition` | `all` | Ablation condition: `all`, `no_log`, `no_metric`, `no_trace` |
| `--max-steps` | 25 | Max orchestrator steps before forced submit |
| `--results-dir` | `results/rca_agent` | Output directory for result JSONs |
| `--work-dir` | cwd | Telemetry CSV save directory (use unique path for parallel runs) |
| `--api-config` | `clients/openrca_rca/api_config.yaml` | LLM API configuration |

### `experiments/telemetry_ablation/run_ablation.py`

| Flag | Default | Description |
|------|---------|-------------|
| `--dataset` | all | `bank`, `telecom`, or `market` |
| `--condition` | all | `all`, `no_log`, `no_metric`, `no_trace` |
| `--max-steps` | 25 | Max steps per problem |
| `--force` | false | Re-run completed experiments |
| `--summary-only` | false | Only generate `RESULTS.md` |

## Claude Runbook (자동 실행용)

이 섹션은 Claude가 이 파일만 읽고도 전체 실험을 병렬로 실행할 수 있도록 작성되었습니다.
사용자가 "실험 실행해줘"라고 하면, 이 절차를 따르세요.

### 사전 조건

- Docker daemon 실행 중 (`docker ps` 로 확인)
- OpenAI API key 설정됨 (`clients/openrca_rca/api_config.yaml` 존재)
- 프로젝트 루트: `AIOpsLab/` 디렉토리

### Step 1: 진행 상황 확인

```bash
python experiments/telemetry_ablation/run_ablation.py --summary-only
```

이 명령으로 현재 완료된 실험과 남은 실험을 확인합니다.
`RESULTS.md`에 결과가 기록됩니다.

### Step 2: 병렬 실행 (11개 프로세스)

**핵심 규칙:**
- 같은 dataset의 다른 condition은 **동시 실행 가능** (컨테이너 격리됨)
- 같은 results-dir에 cb1과 cb2를 **동시 실행 불가** (JSON 파일 충돌)
  → cb1 완료 후 cb2 실행, 또는 별도 results-dir 사용
- 각 프로세스에 고유한 `--work-dir` 필수 (CSV 파일 충돌 방지)
- `telecom_no_log`은 실행하지 않음 (Telecom에 log 데이터 없어 all과 동일)

**Phase 1: Bank(4) + Telecom(3) + Market CB1(4) = 11 프로세스 동시 실행**

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

# Market CB1 (4 conditions)
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition all       --work-dir /tmp/ablation_mcb1_all       --results-dir experiments/telemetry_ablation/results/market_all &
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_log    --work-dir /tmp/ablation_mcb1_no_log    --results-dir experiments/telemetry_ablation/results/market_no_log &
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_metric --work-dir /tmp/ablation_mcb1_no_metric --results-dir experiments/telemetry_ablation/results/market_no_metric &
python clients/run_rca_agent.py --dataset openrca_market_cb1 --condition no_trace  --work-dir /tmp/ablation_mcb1_no_trace  --results-dir experiments/telemetry_ablation/results/market_no_trace &
```

**Phase 2: Market CB2 (Phase 1 완료 후)**

CB1과 같은 results-dir을 사용하므로, CB1이 끝난 후 실행합니다.
(per-problem skip 로직으로 CB1 결과는 자동 보존됩니다.)

```bash
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition all       --work-dir /tmp/ablation_mcb2_all       --results-dir experiments/telemetry_ablation/results/market_all &
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_log    --work-dir /tmp/ablation_mcb2_no_log    --results-dir experiments/telemetry_ablation/results/market_no_log &
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_metric --work-dir /tmp/ablation_mcb2_no_metric --results-dir experiments/telemetry_ablation/results/market_no_metric &
python clients/run_rca_agent.py --dataset openrca_market_cb2 --condition no_trace  --work-dir /tmp/ablation_mcb2_no_trace  --results-dir experiments/telemetry_ablation/results/market_no_trace &
```

### Step 3: 모니터링

실행 중 진행 상황 확인:

```bash
# 결과 JSON 파일 개수 확인
find experiments/telemetry_ablation/results/ -name "*.json" -not -name "metadata.json" | wc -l

# 조건별 완료 현황
for d in experiments/telemetry_ablation/results/*/; do
  name=$(basename "$d")
  count=$(find "$d" -name "*.json" -not -name "metadata.json" 2>/dev/null | wc -l)
  echo "$name: $count problems"
done

# 실행 중인 프로세스 확인
ps aux | grep run_rca_agent | grep -v grep | wc -l

# Docker 컨테이너 상태
docker ps --filter "name=static-dataset" --format "table {{.Names}}\t{{.Status}}"

# 요약 테이블 갱신
python experiments/telemetry_ablation/run_ablation.py --summary-only
```

### Step 4: 에러 처리

- **프로세스 죽음**: 같은 명령어 다시 실행 → per-problem skip으로 이어서 진행
- **Docker container down**: `docker ps`로 확인, 해당 프로세스만 재실행
- **API rate limit**: 잠시 대기 후 재실행 (결과 있는 문제는 자동 스킵)
- **모든 결과 재실행**: `--results-dir`을 다른 경로로 지정

### Step 5: 결과 취합

```bash
python experiments/telemetry_ablation/run_ablation.py --summary-only
cat experiments/telemetry_ablation/results/RESULTS.md
```

### 예상 실행 규모

| 항목 | 값 |
|------|-----|
| 총 문제 수 | ~1,100 (330 problems × 3.3 avg conditions) |
| 문제당 소요 시간 | ~40-60초 |
| 병렬 프로세스 | 11 (Phase 1), 4 (Phase 2) |
| 예상 총 시간 | Phase 1: ~2시간, Phase 2: ~30분 |

## Previous runs

### Run 1 (telemetry_ablation2/)

Results from first run stored in `experiments/telemetry_ablation2/results/`.
These results are **invalid** due to Docker container conflicts — all conditions
for the same dataset shared one container, causing mutual destruction during
parallel execution. ~75 container-down errors, ~400+ wrong time window data.

### Run 2 (current)

Fixed with container isolation (condition in namespace) and proper telemetry
flag overriding. Results in `experiments/telemetry_ablation/results/`.
