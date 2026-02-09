# Static Telemetry Replayer - 최종 설계 문서

## 📋 개요

**Static Telemetry Replayer**는 정적 데이터셋(OpenRCA, Alibaba, ACME 등)을 실시간 텔레메트리로 변환하는 범용 시스템입니다.

### 핵심 기능

1. **다양한 데이터셋 지원**: 어댑터 패턴으로 새로운 데이터셋 쉽게 추가
2. **Query 기반 Time Remapping**: 각 데이터셋의 query 형식에 맞춰 시간 변환
3. **History Bulk Loading**: 시뮬레이션 시작 전 과거 데이터 일괄 적재
4. **선택적 Telemetry 재생**: Trace/Log/Metric을 설정으로 제어
5. **AIOpsLab 표준 준수**: Application 클래스 상속

---

## 🏗️ 아키텍처

```
┌─────────────────────────────────────────────────────────┐
│          Static Telemetry Replayer 시스템                │
└─────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────┐
│  다양한 정적 데이터셋                              │
│  ├─ openrca_dataset/                             │
│  ├─ alibaba_cluster_dataset/                     │
│  └─ acme_cluster_dataset/                        │
└────────────┬─────────────────────────────────────┘
             │
             ▼
┌──────────────────────────────────────────────────┐
│      Dataset Adapter Layer                       │
│  ┌─────────────┬──────────────┬──────────────┐  │
│  │OpenRCAAdapter│AlibabaAdapter│ AcmeAdapter │  │
│  └──────┬──────┴──────┬───────┴──────┬───────┘  │
│         └─────────────┴──────────────┘           │
│              BaseDatasetAdapter                  │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│         Query Parser Layer                       │
│  ┌─────────────┬──────────────┬──────────────┐  │
│  │OpenRCAParser│AlibabaParser │ AcmeParser  │  │
│  └──────┬──────┴──────┬───────┴──────┬───────┘  │
│         └─────────────┴──────────────┘           │
│              BaseQueryParser                     │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│         Time Remapper                            │
│  - Query 기반 시간 매핑                          │
│  - Anchor point 결정                             │
│  - History 범위 계산                             │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│         Bulk Loaders (History)                   │
│  ┌──────────────┬──────────────┬──────────────┐ │
│  │Elasticsearch │Prometheus    │Jaeger        │ │
│  │BulkLoader    │BulkLoader    │BulkLoader    │ │
│  └──────────────┴──────────────┴──────────────┘ │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│         Replayers (Realtime)                     │
│  ┌──────────────┬──────────────┬──────────────┐ │
│  │ Log Replayer │Metric Replayer│Trace Replayer│ │
│  └──────┬───────┴───────┬──────┴──────┬────────┘ │
└─────────┼───────────────┼─────────────┼──────────┘
          │               │             │
          ▼               ▼             ▼
┌──────────────┐  ┌──────────┐  ┌──────────┐
│ Elasticsearch│  │Prometheus│  │  Jaeger  │
└──────────────┘  └──────────┘  └──────────┘
```

---

## 📁 디렉토리 구조

```
aiopslab/service/apps/static_replayer/
├── __init__.py
├── PROPOSAL.md                    # 이 문서
├── README.md                      # 사용 가이드
├── replayer.py                    # StaticReplayer (메인 클래스)
│
├── config/                        # 데이터셋별 설정
│   ├── openrca_bank.json
│   ├── openrca_telecom.json
│   ├── openrca_market_cloudbed1.json
│   ├── openrca_market_cloudbed2.json
│   ├── alibaba_cluster.json
│   └── acme_cluster.json
│
├── adapters/                      # 데이터셋 어댑터
│   ├── __init__.py
│   ├── base.py                    # BaseDatasetAdapter
│   ├── openrca.py                 # OpenRCAAdapter
│   ├── alibaba.py                 # AlibabaAdapter
│   └── acme.py                    # AcmeAdapter
│
├── time_mapping/                  # 시간 매핑 모듈
│   ├── __init__.py
│   ├── base_query_parser.py      # BaseQueryParser + QueryResult
│   ├── openrca_query_parser.py   # OpenRCAQueryParser
│   ├── alibaba_query_parser.py   # AlibabaQueryParser
│   ├── acme_query_parser.py      # AcmeQueryParser
│   └── time_remapper.py          # TimeRemapper
│
├── bulk_loader/                   # 히스토리 일괄 적재
│   ├── __init__.py
│   ├── elasticsearch_bulk.py     # ElasticsearchBulkLoader
│   ├── prometheus_bulk.py        # PrometheusBulkLoader
│   └── jaeger_bulk.py            # JaegerBulkLoader
│
├── replayers/                     # 실시간 재생 엔진
│   ├── __init__.py
│   ├── base_replayer.py          # BaseReplayer
│   ├── log_replayer.py           # LogReplayer
│   ├── metric_replayer.py        # MetricReplayer
│   ├── trace_replayer.py         # TraceReplayer
│   └── requirements.txt
│
└── docker/                        # Docker 배포
    ├── docker-compose.yml
    ├── prometheus.yml
    ├── Dockerfile.replayer
    └── entrypoint.sh

aiopslab/service/metadata/
└── static-replayer.json           # Application 메타데이터
```

---

## 🔧 핵심 컴포넌트

### 1. BaseQueryParser

**역할**: 각 데이터셋의 query 파일을 파싱하여 표준 QueryResult 반환

**구현**: `time_mapping/base_query_parser.py`

**주요 메서드**:
- `parse_task(task_identifier: str) -> QueryResult`
- `list_tasks() -> List[str]`

**표준 출력**: `QueryResult` 객체
```python
QueryResult(
    task_id="task_1",
    time_range={
        'start': 1614841800,
        'end': 1614843600,
        'start_str': "2021-03-04 14:30:00",
        'end_str': "2021-03-04 15:00:00",
        'duration': 1800
    },
    faults=[
        {'timestamp': 1614841020, 'component': 'Mysql02', 'reason': 'high memory'}
    ],
    metadata={'instruction': '...'}
)
```

### 2. TimeRemapper

**역할**: 원본 타임스탬프를 시뮬레이션 타임스탬프로 변환

**구현**: `time_mapping/time_remapper.py`

**시간 매핑 모드**:
- `realtime`: 현재 시간 기준 매핑
- `manual`: 사용자 지정 시간
- `query_based`: query에서 자동 추출

**Anchor Strategy**:
- `fault_start`: 장애 시작 시점
- `fault_detection`: 장애 감지 시점 (record.csv)
- `data_start`: 데이터셋 시작 시점
- `custom`: 사용자 지정

### 3. BulkLoader

**역할**: 시뮬레이션 시작 전 히스토리 데이터 일괄 적재

**구현**:
- `bulk_loader/elasticsearch_bulk.py`: Elasticsearch Bulk API
- `bulk_loader/prometheus_bulk.py`: Prometheus Remote Write
- `bulk_loader/jaeger_bulk.py`: Jaeger Batch Submit

**효과**: 30분 히스토리를 30초 만에 적재

### 4. Replayer

**역할**: 시뮬레이션 시작 시점부터 실시간 재생

**구현**:
- `replayers/log_replayer.py`: Elasticsearch에 로그 전송
- `replayers/metric_replayer.py`: Pushgateway에 메트릭 전송
- `replayers/trace_replayer.py`: Jaeger에 트레이스 전송

**속도 조절**: `speed_factor` (1.0 = 실시간, 10.0 = 10배속)

---

## 📋 설정 파일 형식

### OpenRCA Bank 예시

```json
{
  "dataset_name": "OpenRCA Bank",
  "dataset_type": "openrca",
  "dataset_path": "/openrca_dataset/Bank",
  "namespace": "static-bank",

  "telemetry": {
    "enable_trace": true,
    "enable_log": true,
    "enable_metric": true
  },

  "query": {
    "enable": true,
    "query_file": "query.csv",
    "record_file": "record.csv",
    "task_identifier": "task_1"
  },

  "time_mapping": {
    "mode": "realtime",
    "anchor_strategy": "fault_start",
    "simulation_start_time": null,
    "history_duration_seconds": 1800,
    "enable_bulk_history": true,
    "time_offset_seconds": 0
  },

  "replay_config": {
    "speed_factor": 1.0,
    "loop": false
  },

  "data_mapping": {
    "trace_files": ["trace_span.csv"],
    "log_files": ["log_service.csv"],
    "metric_files": ["metric_app.csv", "metric_container.csv"]
  }
}
```

### Alibaba Cluster 예시

```json
{
  "dataset_name": "Alibaba Cluster",
  "dataset_type": "alibaba",
  "dataset_path": "/alibaba_cluster_dataset",
  "namespace": "static-alibaba",

  "telemetry": {
    "enable_trace": false,
    "enable_log": false,
    "enable_metric": true
  },

  "query": {
    "enable": true,
    "task_identifier": null,
    "auto_select": "failed_jobs"
  },

  "time_mapping": {
    "mode": "realtime",
    "anchor_strategy": "data_start",
    "history_duration_seconds": 1800,
    "enable_bulk_history": true
  },

  "replay_config": {
    "speed_factor": 10.0,
    "sample_size": 1000,
    "loop": false
  },

  "data_mapping": {
    "metric_files": [
      "pai_machine_metric.csv",
      "pai_sensor_table.csv"
    ]
  }
}
```

---

## 🚀 사용 방법

### 기본 사용

```python
from aiopslab.service.apps.static_replayer import StaticReplayer

# OpenRCA Bank 데이터셋 재생
replayer = StaticReplayer("openrca_bank")
replayer.deploy()

# AIOpsLab Observer로 데이터 수집
from aiopslab.observer.observe import collect_traces, collect_logs, collect_metrics

collect_traces(start_time, end_time)
collect_logs(start_time, end_time)
collect_metrics(start_time, end_time)

# 정리
replayer.cleanup()
```

### 10배속 재생

```python
replayer = StaticReplayer("openrca_telecom_fast")
replayer.deploy()
# 30분 데이터를 3분만에 재생
```

### 새 데이터셋 추가

1. QueryParser 구현 (`time_mapping/my_dataset_query_parser.py`)
2. Adapter 구현 (`adapters/my_dataset.py`)
3. 설정 파일 작성 (`config/my_dataset.json`)

---

## 🔄 타임라인 예시

```
원본 OpenRCA Bank (2021-03-04):
14:00        14:30              14:57           15:00
  │            │                  │               │
  ├────────────┼──────────────────┼───────────────┤
 정상         장애 시작          Mysql02 장애     종료
(history)     (anchor)          (root cause)

                     ▼ Time Remapping

시뮬레이션 (2026-02-09):
17:30        18:00              18:27           18:30
  │            │                  │               │
  ├────────────┼──────────────────┼───────────────┤
Bulk Load   Replay Start        Mysql02 Fault   End
(30초)      (실시간 재생)        (27분 후)

처리 방식:
┌──────────────────┬──────────────────┬────────────────────┐
│ 시간 구간        │ 데이터 적재 방식  │ 소요 시간          │
├──────────────────┼──────────────────┼────────────────────┤
│ 17:30 ~ 18:00    │ Bulk API         │ ~30초              │
│ 18:00 ~ 18:30    │ Realtime Replay  │ 30분 (1x)          │
│                  │                  │  3분 (10x)         │
└──────────────────┴──────────────────┴────────────────────┘
```

---

## ✅ 구현 단계

### Phase 1: 기본 구조 (1주)
- [x] 디렉토리 구조 생성
- [ ] BaseQueryParser 구현
- [ ] BaseDatasetAdapter 구현
- [ ] QueryResult 클래스
- [ ] 설정 파일 로더

### Phase 2: OpenRCA 지원 (1주)
- [ ] OpenRCAQueryParser 구현
- [ ] OpenRCAAdapter 구현
- [ ] TimeRemapper 구현
- [ ] OpenRCA 설정 파일 작성

### Phase 3: Bulk Loading (1주)
- [ ] ElasticsearchBulkLoader 구현
- [ ] PrometheusBulkLoader 구현
- [ ] JaegerBulkLoader 구현
- [ ] Bulk loading 통합 테스트

### Phase 4: Realtime Replay (1주)
- [ ] LogReplayer 구현
- [ ] MetricReplayer 구현
- [ ] TraceReplayer 구현
- [ ] Docker Compose 구성

### Phase 5: Alibaba 지원 (1주)
- [ ] AlibabaQueryParser 구현
- [ ] AlibabaAdapter 구현
- [ ] Alibaba 설정 파일 작성
- [ ] 통합 테스트

### Phase 6: 문서화 및 최적화 (1주)
- [ ] README.md 작성
- [ ] 사용 예제 작성
- [ ] 성능 최적화
- [ ] 전체 통합 테스트

---

## 🎯 성공 기준

1. **기능성**
   - ✅ OpenRCA 4개 데이터셋 모두 재생 가능
   - ✅ Alibaba 데이터셋 재생 가능
   - ✅ Time remapping 정확도 (±1초 이내)
   - ✅ History bulk loading (30초 이내)

2. **성능**
   - ✅ 메모리 사용량 < 4GB
   - ✅ 10배속 재생 안정성
   - ✅ Bulk loading 실패율 < 1%

3. **확장성**
   - ✅ 새 데이터셋 추가 시 < 200줄 코드
   - ✅ 새 QueryParser 추가 시 < 100줄 코드

4. **통합성**
   - ✅ 기존 AIOpsLab observe.py와 호환
   - ✅ Application 표준 준수
   - ✅ Docker Compose로 원스톱 배포

---

## 📚 참고 자료

- OpenRCA 데이터셋: `/openrca_dataset/`
- Alibaba 데이터셋: `/alibaba_cluster_dataset/`
- AIOpsLab Observer: `aiopslab/observer/observe.py`
- Application 기본 클래스: `aiopslab/service/apps/base.py`

---

**작성일**: 2026-02-09
**작성자**: Claude Sonnet 4.5
**버전**: 1.0 (Final)
