# AGENTS.md

이 문서는 `Network-Backend-Article` 저장소 전체에 적용된다.

## 1. 기본 원칙

작업 시작 시 `RFVisualizer-Docs` 전체를 읽지 않는다.

기본적으로 다음만 확인한다.

1. 이 `AGENTS.md`
2. 사용자가 지정한 파일
3. 수정 대상 Backend 코드, 설정, 테스트
4. 필요할 경우 `README.md` 또는 `TESTING.md`의 관련 절

중앙 문서 저장소:

- GitHub: https://github.com/2026-PNU-CSE-GRAD-RFVisualizer/RFVisualizer-Docs
- 권장 로컬 위치: `../RFVisualizer-Docs`

## 2. 중앙 문서 선택 규칙

| 작업 상황 | 읽을 문서 | 범위 |
|---|---|---|
| 프로젝트 목표나 Backend 책임 판단 | `PROJECT.md` | 목표, 파트 책임, 설계 원칙 |
| 현재 구현 상태나 Realtime 활성 여부 판단 | `CURRENT_STATUS.md` | 전체 요약과 네트워크 절 |
| MQTT·RSSI·좌표·Export·WebSocket 등 파트 간 계약 변경 | `INTERFACE.md` | 관련 인터페이스 절과 변경 절차 |
| Backend 전체 구조나 실험 모델 파악 | `network/NETWORK.md` | 관련 기능 절만 |
| 내부 버그 수정·Query 최적화·테스트 보강 | 중앙 문서 불필요 | 대상 코드와 로컬 테스트만 확인 |

다음 작업에서는 `INTERFACE.md` 확인이 필수다.

- MQTT Topic 또는 Payload 변경
- Field Alias 제거/추가
- `rssi`, `rssi_raw`, Timestamp 의미 변경
- Node ID 또는 좌표 형식 변경
- Invalid Sample 정책 변경
- `corrected_rssi` 계산 변경
- Export 폴더, CSV, JSON Schema 변경
- WebSocket Frame 또는 PositionEstimate 변경
- Embedded Producer나 Graphics Consumer에 영향을 주는 변경

문서는 관련 제목과 주변 절만 읽는다. 작업과 무관한 과거 보고서나 계획서는 열지 않는다.

## 3. 판단 우선순위

1. 현재 동작 코드와 통과한 테스트
2. 공통 계약이 관련되면 `INTERFACE.md`
3. 현재 상태가 관련되면 `CURRENT_STATUS.md`
4. Backend 설계가 관련되면 `network/NETWORK.md`
5. 이 저장소의 `README.md`, `TESTING.md`
6. 과거 계획과 보고서

코드와 중앙 문서가 충돌하면 구현 결함인지 문서 미갱신인지 확인한다.

## 4. 네트워크/백엔드 파트 경계

이 저장소의 책임:

- MQTT 구독과 Payload 파싱
- Field 정규화
- Timestamp, Sequence, Packet Loss, Online 상태
- Invalid Sample 보존
- Experiment, Point, Session, Node Assignment
- Device Offset
- SQLite와 JSONL
- Graphics-ready CSV/JSON Export
- Quality Check와 Rehearsal
- 선택적 Realtime Window와 WebSocket
- PositionEstimate 인터페이스

ESP32 Firmware와 Graphics RF Solver를 중복 구현하지 않는다.

## 5. 현재 실행 모드

기본 실행 모드:

```env
ENABLE_REALTIME=false
```

논문·측정 경로의 기준 저장소:

```text
SQLite + JSONL
```

다음 항목을 수정하거나 설명할 때만 `CURRENT_STATUS.md`의 네트워크 절을 읽는다.

- 200 ms Realtime Window
- `WS /frames`
- `GET /position/latest`
- 실제 Position 추정
- Graphics Viewer 연결
- Handheld Position Update

PositionEstimate가 Placeholder라면 임의 위치를 실제 결과처럼 반환하지 않는다.

## 6. 유지할 데이터 의미

### MQTT

```text
rssi/#
gateway/#
status/+/lwt
```

```text
rssi      = Filtered RSSI, dBm
rssi_raw  = Raw RSSI, dBm
status    = 0이면 정상
timestamp = Unix Epoch millisecond
```

### 호환 Alias

| 의미 | 허용 필드 |
|---|---|
| Filtered RSSI | `rssi_filtered_dbm`, `rssi_filtered`, `rssi` |
| Raw RSSI | `rssi_raw_dbm`, `rssi_raw` |
| AP Channel | `ap_channel`, `channel` |
| 오류 상태 | `error_flags`, `status` |

Alias 제거 시 현재 Producer와 기존 데이터의 영향을 확인한다.

### Invalid Sample

구조 자체가 손상된 메시지를 제외하면 가능한 한 Raw 데이터를 보존한다.

```json
{
  "valid": false,
  "invalid_reason": "..."
}
```

Invalid Sample은 Raw 저장에 남기고 대표 RSSI와 보정에서는 제외한다.

### Device Offset

```text
corrected_rssi = median_filtered + device_offset_db
```

이 계산은 Graphics 결과에 직접 영향을 주므로 단독으로 변경하지 않는다.

## 7. Experiment와 Export

Point Role:

| Role | 목적 |
|---|---|
| `offset` | 장치 편차 계산 |
| `calibration` | RF 보정 |
| `test` | 평가 전용 |

Sample 위치는 Session 이름이 아니라 Node Assignment를 따른다.

기본 Export:

```text
experiments/<experiment_id>/
├── raw/measurements_raw.csv
├── processed/measurements_summary.csv
├── processed/calibration_points.csv
├── processed/test_points.csv
├── config/points.csv
├── config/tx_rx.json
├── config/device_offsets.json
├── config/sessions.json
├── qc_report.json
└── README.md
```

Graphics 기본 입력:

```text
x, y, z, corrected_rssi
```

Calibration과 Test 데이터를 섞지 않는다.

## 8. 좌표와 실험 분리

- 단위: meter
- `+Z`: 위쪽
- 원점과 수평축: Experiment별 설정
- 강의실과 복도 좌표를 섞지 않는다.
- Placeholder `(0,0,0)`을 실제 배치로 간주하지 않는다.
- TX와 Point가 동일한 Frame ID를 쓰는지 확인한다.
- Payload 위치와 Backend Assignment가 충돌하면 기준을 명시한다.
- QC 기대 Point 수를 특정 실험 기준으로 전역 고정하지 않는다.

## 9. 코드 변경 규칙

- `ENABLE_REALTIME=false`에서도 측정 경로가 동작해야 한다.
- Parser와 계산 로직을 Broker 없이 테스트 가능하게 유지한다.
- Raw 데이터를 재현 가능하게 보존한다.
- DB Schema 변경에는 Migration 또는 명확한 초기화 절차를 제공한다.
- Sample 위치를 Session 이름에서 추론하지 않는다.
- Node Timestamp와 Server Receive Time을 구분한다.
- Secret과 실제 내부 주소를 Commit하지 않는다.
- `data/`와 대규모 `experiments/` 출력을 Source처럼 Commit하지 않는다.
- 측정 중 자동 Reload로 Session을 중단하지 않는다.
- 오류를 성공 응답으로 숨기지 않는다.
- 하위 호환 제거 시 영향과 마이그레이션을 기록한다.

## 10. 검증

변경 영역에 해당하는 검사만 수행한다.

### Parser

- 개별 RSSI와 Gateway Batch
- Raw/Filtered Alias
- x10 Scale
- Invalid RSSI와 Error Flag
- Timestamp Skew
- Sequence 누락/중복
- BSSID와 Channel

### Experiment

- Start/End
- Point Import
- Node Assignment
- Session Start/Stop
- 고정 Node 누적
- 이동 Node 재배치
- Device Offset
- Calibration/Test 분리

### 저장과 Export

- JSONL
- SQLite
- Raw/Summary CSV
- Calibration/Test CSV
- TX/RX와 Device Offset JSON
- QC Report
- `corrected_rssi`

### Realtime

- `ENABLE_REALTIME=false` 회귀
- 200 ms Bucket
- Missing Node
- 여러 WebSocket Client
- 느린 Client와 재연결
- Position Placeholder

권장 테스트 명령은 `TESTING.md`를 따른다. 실행하지 못한 테스트는 완료로 표시하지 않는다.

## 11. 중앙 문서 갱신 조건

| 변경 | 갱신 문서 |
|---|---|
| MQTT, RSSI, 좌표, Export, WebSocket 계약 | `INTERFACE.md` |
| 구현 상태나 활성 모드 | `CURRENT_STATUS.md` |
| Backend 구조·Experiment 모델 | `network/NETWORK.md` |
| 프로젝트 목표·파트 책임 | `PROJECT.md` |

단순 내부 버그 수정, 성능 개선, 동작이 변하지 않는 리팩터링은 중앙 문서를 수정하지 않는다.

## 12. 결과 보고

관련된 항목만 보고한다.

- 변경 파일과 이유
- API/Topic/Schema 변경 여부
- 하위 호환과 DB Migration
- 실행한 테스트와 Rehearsal
- 생성한 Export
- 사용한 Experiment 구성
- 실물/Realtime 미검증 항목
- 중앙 문서 변경 여부
