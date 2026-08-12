# 중앙 문서 변경안 (INTERFACE.md / network/NETWORK.md)

최종 실험(Run/TestSegment) 재설계는 Graphics 파트가 소비하는 Export 계약을 바꾼다.
아래 내용을 `RFVisualizer-Docs/INTERFACE.md` §8(Export)·§7(Experiment 모델), `network/NETWORK.md`
§10~11 에 반영한다(§13 변경 절차: PR + 그래픽스 합의).

## 1. Experiment 모델

```
Experiment
├── OffsetRun (phase: pre | post)        # 본 실험과 분리
├── ExperimentRun (direction, pass_index) # 정/역방향 회차
│   └── TestSegment (order_index, attempt_index, [rec_start, rec_end))
├── Measurement Point / Node Assignment
└── Measurement (run_id, segment_id)
```

- ExperimentRun 은 자동 종료가 없다(사용자가 종료). TestSegment 만 안정화→기록→완료 자동 전이.
- 샘플 소속은 **`[recording_started_at_ms, recording_ended_at_ms)` 시간 범위**로 판정한다.
- 구간 매칭 기준 시각은 **`server_ts_ms`** (Backend 수신 시각). 브라우저 시간 아님.

## 2. 사전·사후 OffsetRun

- `pre` = 본 실험 보정에 사용(`corrected_rssi`). `post` = 편차 변화(drift) 확인용, 재보정 안 함.
- 서로 다른 `offset_run_id` 로 저장. `experiment_run.pre_offset_run_id` / `post_offset_run_id`.

## 3. Measurement 식별 필드

```
run_id        본 실험 Run (또는 offset_run_id)
segment_id    TestSegment (C1~C4 이동 구간·offset 은 NULL)
direction     forward | reverse
pass_index    회차 번호
attempt_index 재측정 시도 번호 (test_segment)
```

- **Legacy vs 신규 구분**: `run_id IS NULL AND segment_id IS NULL` 이면 구 세션 모델 Legacy 데이터.

## 4. Export 산출물 변경

```
processed/test_points.csv                # 컬럼: run_id, direction, pass_index, segment_id,
                                         #       point_id, attempt_index, recording_started/ended_at_ms,
                                         #       node_id, sample_count, median_filtered,
                                         #       device_offset_db, corrected_rssi, x, y, z
processed/calibration_by_test_window.csv # 각 Test 와 동일 시간대 C1~C4 (segment_id 로 연결)
processed/calibration_points.csv         # Run 전체 진단용(Test별 동시간 아님)
config/runs.json, test_segments.json     # 신규
config/device_offsets.json               # pre / post / drift(post-pre) 포함
```

### Graphics 파트 연결 기준

```
test_points.csv.segment_id  ↔  calibration_by_test_window.csv.segment_id
```

- 한 `segment_id` 에 T 1행(test_points) + C1~C4 4행(calibration_by_test_window)이 대응한다.
- `corrected_rssi = median_filtered + device_offset_db` (device_offset 은 그 Run 의 **사전** offset).
- 정방향 T1 과 역방향 T1, 재측정 T1(attempt_index) 은 서로 다른 행 — 합치지 않는다.
- `calibration_points.csv`(전체 Run 평균)를 모든 Test 에 공통 적용하지 않는다.

## 5. 확인 요청 (그래픽스 파트)

샘플 Export 를 전달할 테니, 다음 연결을 실제로 수행할 수 있는지 확인 바람:

1. `test_points.csv` 각 행의 `segment_id` 로 `calibration_by_test_window.csv` 의 C1~C4 4행을 조인
2. `corrected_rssi` 를 IDW/Residual 입력으로, `test_points` 를 평가 전용으로 분리 사용
3. `direction`/`pass_index`/`attempt_index` 로 정·역·재측정 회차 구분
