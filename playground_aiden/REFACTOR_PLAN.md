# Static Dataset 리팩토링 계획

## Context

현재 Static Replayer는 Adapter 패턴, 외부 인프라(ES/Prometheus/Jaeger), Observer 의존성 등 불필요한 복잡성이 있음.
edit.md에 따라 아키텍처를 단순화하고 AIOpsLab의 기존 패턴(problems/orchestrator/actions)에 맞게 재구조화함.

**핵심 변경 방향:**
- Adapter 레이어 제거 (불필요한 추상화)
- ES/Prometheus/Jaeger 인프라 띄우지 않음
- App이 telemetry를 Docker 내부 저장소에 저장 → API로 접근
- `service/static_app.py` 서비스 클라이언트 (dock.py/kubectl.py 패턴)
- static_problems/, static_orchestrator, static_actions 분리
- Docker 파일을 aiopslab-applications/로 이동

**이름 변경:**
- `static_replayer` → `static_dataset`
- `StaticReplayer` → `StaticDataset`
- `telemetry_store/` → `service/static_app.py` (기존 서비스 패턴)
- `static-replayer.json` → `static-dataset.json`
- `STATIC_REPLAYER_METADATA` → `STATIC_DATASET_METADATA`

---

## 새로운 아키텍처

```
┌──────────────────────────────────────────────────────────────────┐
│                    새로운 디렉토리 구조                            │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  aiopslab-applications/static_dataset/     ← Docker 배포 파일    │
│  ├── docker-compose.yml                                          │
│  ├── Dockerfile                                                  │
│  └── entrypoint.sh                                               │
│                                                                  │
│  aiopslab/service/static_app.py            ← 서비스 클라이언트    │
│  (dock.py, kubectl.py와 같은 레벨)                                │
│                                                                  │
│  aiopslab/service/apps/static_dataset/     ← App 관리 (서비스)   │
│  ├── dataset.py           (StaticDataset: namespace, config,     │
│  │                         deploy/cleanup)                       │
│  ├── config/              (데이터셋 설정 JSON)                    │
│  └── time_mapping/        (시간 매핑 로직)                        │
│      ├── time_remapper.py                                        │
│      ├── base_query_parser.py                                    │
│      └── openrca_query_parser.py                                 │
│  (adapters/ 삭제, bulk_loader/ 삭제, replayers/ 삭제)             │
│  (docker/ 삭제 → aiopslab-applications/으로 이동)                 │
│                                                                  │
│  aiopslab/orchestrator/base.py             ← 새로운 Base         │
│  aiopslab/orchestrator/orchestrator.py     ← 기존 (base 상속)    │
│  aiopslab/orchestrator/static_orchestrator.py ← Static 전용      │
│                                                                  │
│  aiopslab/orchestrator/static_problems/    ← 문제 정의            │
│  ├── __init__.py                                                 │
│  ├── registry.py                                                 │
│  ├── openrca/                                                    │
│  │   ├── __init__.py                                             │
│  │   ├── base_task.py     (OpenRCA 공통 로직)                     │
│  │   ├── bank.py          (Bank Detection/Localization/Analysis) │
│  │   ├── telecom.py                                              │
│  │   ├── market_cloudbed1.py                                     │
│  │   └── market_cloudbed2.py                                     │
│  └── alibaba/                                                    │
│      ├── __init__.py                                             │
│      └── cluster.py                                              │
│                                                                  │
│  aiopslab/orchestrator/static_actions/     ← Static 전용 Actions │
│  ├── __init__.py                                                 │
│  ├── base.py              (StaticApp을 통한 접근 API)             │
│  └── detection.py         (submit 등)                            │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 상세 구현 단계

### Step 1: service/static_app.py 생성 (서비스 클라이언트)

**새 파일:** `aiopslab/service/static_app.py`
- `StaticApp` 클래스 (dock.py의 Docker, kubectl.py의 KubeCtl과 같은 레벨)
- Docker volume CSV에서 telemetry 읽기/쓰기:
  ```python
  class StaticApp:
      def __init__(self, base_path: str):
          self.base_path = base_path

      def get_logs(self, namespace, service, start_time=None, end_time=None) -> str
      def get_metrics(self, namespace, duration_minutes=5) -> str
      def get_traces(self, namespace, duration_minutes=5) -> str
      def store_telemetry(self, namespace, telemetry_type, data, time_remapper=None) -> int
  ```

### Step 2: aiopslab-applications/static_dataset/ 생성 (Docker 배포)

**파일:** `aiopslab-applications/static_dataset/docker-compose.yml`
- 데이터셋 파일을 Docker volume으로 마운트하는 컨테이너 정의
- ES/Prometheus/Jaeger 없이, 단순한 데이터 저장 컨테이너

**파일:** `aiopslab-applications/static_dataset/Dockerfile`
- Python 기반 컨테이너
- 데이터셋 로드 + 시간 변환 + 내부 저장소에 저장
- Volume mount 경로: `/data/telemetry/` (logs/, metrics/, traces/)

### Step 3: static_replayer/ → static_dataset/ 리네이밍 및 단순화

**리네이밍:** `service/apps/static_replayer/` → `service/apps/static_dataset/`
**리네이밍:** `replayer.py` → `dataset.py`, `StaticReplayer` → `StaticDataset`

**dataset.py 단순화:**
- `_create_adapter()` 삭제
- `_start_infrastructure()` 삭제
- `_bulk_load_history()` 삭제
- `_start_inprocess_replayer()` 삭제
- `deploy()`: Docker Compose로 데이터 컨테이너만 시작
- config 로딩과 namespace 관리에 집중

**삭제:** adapters/, bulk_loader/, replayers/, docker/
**유지:** config/, time_mapping/

### Step 4: Orchestrator 리팩토링

**새 파일:** `aiopslab/orchestrator/base.py`
- 기존 orchestrator.py에서 공통 메서드 추출

**수정:** `orchestrator.py` → BaseOrchestrator 상속

**새 파일:** `static_orchestrator.py` → Static 전용

### Step 5: static_problems/ 디렉토리 생성

**registry.py** + 데이터셋별 폴더 (openrca/, alibaba/)
- `self.app = StaticDataset("openrca_bank")`
- History telemetry를 Docker 내부 저장소에 미리 적재
- CSV timestamp를 Docker 기준으로 변환

### Step 6: static_actions/ 디렉토리 생성

**base.py:** StaticApp 서비스를 사용하는 Actions
**detection.py:** submit 등

### Step 7: 기존 코드 정리

- paths.py: STATIC_DATASET_METADATA
- metadata: static-dataset.json
- config/*.json 형식 단순화

---

## 데이터 흐름 (새 아키텍처)

```
1. static_orchestrator.init_problem("openrca_bank-detection-1")
   │
   ├── registry에서 OpenRCABankDetection 인스턴스 생성
   ├── prob.app = StaticDataset("openrca_bank")
   ├── prob.app.deploy()
   │   └── Docker Compose로 데이터 컨테이너 시작
   │       └── 데이터셋 CSV 로드 + timestamp 변환 + 저장
   │
   ├── prob.inject_fault() → no-op
   └── prob.start_workload() → no-op

2. Agent가 static_actions API 호출:
   │
   ├── get_logs("static-bank", "Mysql02")
   │   └── StaticApp().get_logs() → Docker volume CSV 읽기
   │
   ├── get_metrics("static-bank", 5)
   │   └── StaticApp().get_metrics() → Docker volume CSV 읽기
   │
   └── submit("Yes")
       └── eval() → ground truth와 비교

3. static_orchestrator.start_problem() 완료
   └── prob.app.cleanup() → Docker 컨테이너 정리
```
