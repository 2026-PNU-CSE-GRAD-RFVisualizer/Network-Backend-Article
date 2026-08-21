# 최종 실험 흐름 (Run / TestSegment)

기존 "세션 순차 측정"을 **Run + TestSegment** 모델로 교체했다. C1~C4 는 Run 시작부터
종료까지 끊기지 않고 기록되고, 이동 센서 T 만 위치를 옮기며 위치당 2분씩 기록한다.
각 Test 는 **동일 시간 범위의 C1~C4** 와 매칭된다(전체 평균을 공통 적용하지 않음).

## 상태 모델

```
OffsetRun (본 실험과 분리)   phase = pre | post
ExperimentRun : running → completed | interrupted | discarded   (자동 종료 없음)
TestSegment   : stabilizing → recording → completed | interrupted | discarded
```

### 사전·사후 Offset

- **사전(pre)**: 본 실험 전 5대 공동 측정. 본 실험 RSSI 보정(`corrected_rssi`)에 사용.
- **사후(post)**: 본 실험 후 다시 5대 공동 측정. **재보정하지 않고** 편차 변화(drift) 확인용.
- 사전·사후는 서로 다른 `offset_run_id` 로 저장되어 사후가 사전값을 덮어쓰지 않는다.
  (`device_offset` PK = `(offset_run_id, node_id)`)
- `experiment_run.pre_offset_run_id` = 실제 적용한 사전 출처, `post_offset_run_id` = 사후(없으면 NULL).
- QC/`device_offsets.json` 에 node별 `pre_device_offset_db`, `post_device_offset_db`,
  `device_offset_drift_db = post - pre` 를 기록한다. 임의 합격 임계값은 만들지 않는다.
  사전 또는 사후가 없으면 명확히 경고한다.

- 한 번에 활성 Run 1개, 활성 TestSegment 1개.
- 샘플 소속은 in-memory 포인터가 아니라 **저장된 시간 범위**로 판정
  (`recording_started_at_ms <= server_ts_ms < recording_ended_at_ms`).
  MQTT 지연이 있어도 올바른 Segment 로 저장된다.

## 현장 절차

```
1. 실험 시작 (experiment_id / BSSID / 채널)
2. 센서 배정: C1~C4 = 고정 4대, T = 이동 1대  (node_id 는 하이픈 형식: node-01 …)
3. Offset: 5대를 한 자리에 모음 → 측정 시작 → 종료 → 계산
4. TX(AP) 좌표+채널 등록  +  측정 좌표(points.csv) 등록   ← 아래 "좌표 등록(필수)" 참고
5. 방향 선택(forward/reverse) → 전체 실험 시작 (Run running, C1~C4 연속 기록 시작)
6. 각 위치에서:  T 이동 → [위치 도착·안정화 시작] → 20초 안정화 → 2분 자동 기록 → 자동 완료
   (다음 위치는 사람이 이동 후 다시 버튼)
7. 마지막 위치 완료 → 전체 실험 종료 (Run completed)
8. (선택) 역방향 Run 반복
9. CSV 내보내기 + 품질 점검
```

- 이동·안정화 중 T 데이터는 Test 에 넣지 않는다(JSONL 원본은 보존).
- 재측정: [버리고 재측정] → 이전 Segment 는 superseded, 새 Segment 는 attempt_index+1.
  다른 위치·다른 회차·C1~C4 연속 원본은 삭제되지 않는다.

### 좌표 등록 (필수 — step 4)

**등록하지 않으면 Test 10곳의 좌표가 전부 `(0,0,0)` 으로 저장된다.** 이동 노드(node-02)의
MQTT payload 좌표는 `(0,0,0)` 고정이므로, Export 는 `point` 테이블의 등록 좌표를 정답으로 쓴다
(`export.py` 가 `point_id` 기준으로 join). 등록을 잊으면 그래픽스 IDW 가 무의미해지는데
**겉으로는 정상 종료된다.**

1. **측정 좌표 등록** — 측정 페이지 UI 또는 API 로 `points.csv` 일괄 등록:
   ```
   POST /experiment/points/import
   ```
   `points.csv` 헤더: `point_id, point_role, pos_x, pos_y, pos_z, note` (`x/y/z` 도 인식).
   계획서 §5 표와 일치하는 `points.csv` 를 사용한다(cal-01~04, test-01~10).
   **현장에서 C2/C3 을 예비 좌표로 옮기면 `points.csv` 를 고쳐 다시 등록한다.**
   `Embedded/node_positions.json` 은 건드리지 않는다(좌표 출처를 하나로 유지).

2. **TX(AP) 등록 — 채널 포함**:
   ```
   POST /experiment/tx  { ..., "ap_channel": 6 }   # 현장 AP 실제 채널
   ```
   `ap_channel` 을 넣어야 `config/tx_rx.json` 에 기록되어 완료 조건 §10-3 을 충족한다.
   (측정 행의 `ap_channel` 은 임베디드 payload 수정이 들어가야 채워진다.)

3. **검증(첫 Segment 직후)**: 첫 TestSegment 완료 후 한 번 `POST /experiment/export` 하여
   `processed/test_points.csv` 의 `x` 가 위치마다 다른 값인지 확인한다. 전부 `0` 이면 등록 누락이다.

## API

| 메서드·경로 | 설명 |
|---|---|
| `POST /experiment/start` `/end` | 실험 시작·종료 |
| `POST /experiment/assign` | 노드 역할 배정(calibration/test) |
| `POST /offset-run/start` `{phase:"pre"\|"post"}` `/stop` | 사전·사후 Offset 측정 |
| `POST /experiment/offsets/compute` `{phase\|offset_run_id}` | 장치 편차 계산(해당 OffsetRun) |
| `POST /run/attach-post-offset` `{offset_run_id}` | 사후 Offset 을 Run 에 연결(drift용) |
| `POST /run/start` `{direction, pass_index}` | 본 실험 Run 시작 |
| `POST /run/end` | Run 종료 |
| `GET  /run/current` | 현재 Run·Segment·노드 상태(UI 폴링) |
| `POST /test-segment/prepare` `{point_id, order_index, stabilization_seconds, recording_seconds}` | 안정화→기록 시작 |
| `POST /test-segment/stop` | 현재 Test 즉시 종료(정상 완료) |
| `POST /test-segment/discard` | 버리고 재측정 |
| `POST /experiment/tx` `/points/import` `/export` | TX(+채널)·좌표·내보내기 |
| `GET  /experiment/runs` | Run 목록 |

상태 위반(중복 Run/Segment 시작, 활성 Segment 중 Run 종료, Run 없이 Segment, Offset/Run 동시)은
**HTTP 409**. 조용히 덮어쓰지 않는다.

구 세션 API(`/session/start` `/stop` `/current`)는 **제거되어 HTTP 410 Gone** 을 반환한다.
단일 세션 경로로는 새 실험 데이터를 만들 수 없다.

## 저장 (SQLite, user_version 2)

- `experiment_run`, `test_segment`, `offset_run` 테이블 신규.
- `measurement.run_id`, `measurement.segment_id` 열.
  - C1~C4 이동 구간: `run_id` 있음, `segment_id=NULL` / Test 동시간: 둘 다 있음
  - Offset: `run_id = offset_run_id`, `point_role='offset'`
  - Legacy(기존 DB): 둘 다 NULL
- `device_offset` PK = `(offset_run_id, node_id)` — 사전·사후 공존(사후가 사전 안 덮어씀).
- **마이그레이션**: v0→v1 (measurement 열), v1→v2 (offset_run·device_offset 재구성·experiment_run pre/post).
  기존 device_offset 행은 `offset_run_id='legacy:<experiment_id>'` 로 보존. DB 삭제 불필요.

## Export (`experiments/<experiment_id>/`)

```
processed/test_points.csv                TestSegment 대표값(정/역방향·재측정 별도 행) — 평가 전용
processed/calibration_by_test_window.csv 각 Test 와 동일 시간대 C1~C4 — IDW/Residual 입력
processed/calibration_points.csv         Run 전체 Calibration 진단용(동시간 아님)
processed/measurements_summary.csv       전체 위치·노드 대표값
raw/measurements_raw.csv                 전체 시계열(run_id/segment_id/direction/pass_index)
config/runs.json, test_segments.json, device_offsets.json, points.csv, tx_rx.json
qc_report.json, README.md
```

`segment_id` 로 `test_points.csv` ↔ `calibration_by_test_window.csv` 연결.
`corrected_rssi = median_filtered + device_offset_db`.

### QC 가 잡는 항목 (좌표/순서/채널)

- **좌표 미등록/원점**: 등록되지 않았거나 `(0,0,0)` 인 Test 위치는 `problems` 로 실패.
- **정/역 순서**: `point_id` 끝 숫자로 오름/내림차순 검사(`test-01`·`T1` 형식 모두).
- **AP 채널 누락**: 측정 행 `ap_channel` 이 비면 경고(임베디드 payload 수정 필요).

## 설정 (`.env` / config.py)

```
rssi_min                   = -110     # 약전계 지점(-100~-110dBm) 무효 방지. 임베디드 4곳도 -110 이어야 유효.
test_stabilization_seconds = 20
test_recording_seconds     = 120
expected_test_points       = 10
expected_calibration_nodes = 4
```

## 재시작 처리

- 브라우저 새로고침: Backend 상태(Run·Segment 남은 시간)를 `/run/current` 로 복원. 타이머 재시작 안 함.
- Backend 재시작: 열려 있던 Run/Segment 를 `interrupted` 로 표시(정상 완료로 숨기지 않음). SQLite·JSONL 보존.
- **측정 중 `uvicorn --reload` 금지** (리로드 시 상태 초기화).

## 테스트 / 리허설

```powershell
py tests\test_migration.py         # 스키마 마이그레이션(기존 DB 보존)
py tests\test_run_flow.py          # 상태 규칙·시간 매칭
py tests\test_pipeline_run.py      # 종단 저장(C1~C4 연속·T 기록창)
py tests\test_export_run.py        # Export/QC·동시간 매칭·폐기 제외
py tests\test_offset_prepost.py    # 사전/사후 offset 분리·drift·마이그레이션
py rehearsal.py --reverse          # 사전·사후 offset + 정+역방향 in-process 리허설
```

실제 타이밍 리허설: `py rehearsal.py --stab 20 --rec 120` (느림).

### 측정 직전 실장비 5대 리허설 (미검증 유일 항목)

1~5 수정(rssi_min=-110 / 좌표 join / QC / 좌표 등록 / TX 채널)을 모두 반영한 상태로 최소 1회:

1. pre-offset OffsetRun (5대 같은 위치, 5분) → `POST /experiment/offsets/compute {phase:"pre"}`
2. `POST /run/start {direction:"forward", pass_index:1}`
3. TestSegment 2~3개만 짧게 (`stabilization 5s / recording 10s`)
4. `POST /experiment/export` 후 확인:
   - `processed/test_points.csv` 의 x,y,z 가 위치별로 다른가
   - `raw/measurements_raw.csv` 에 `ap_channel` 이 채워졌는가
   - `qc_report.json` 의 `problems` 가 비었는가
5. 브라우저 새로고침 / MQTT 재연결 / Backend 중단 실동작 확인.
