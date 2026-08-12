# Network-Backend-Article

RFVisualizer 프로젝트의 **네트워크/백엔드 파트** (논문용 저장소).
여러 ESP32 노드의 RSSI를 MQTT로 수집 → 검증 → 저장 → 분석용 CSV로 내보낸다.

현재 기본 모드는 **논문 실험** (`ENABLE_REALTIME=false`).
실시간 시각화 경로는 `backend/realtime/` 에 격리되어 있고 플래그로 켠다.

## 문서

파트 경계·데이터 계약·실험 모델·프로젝트 배경은 이 저장소에 중복 기재하지 않는다.

- 작업 규칙: 이 저장소 루트의 `AGENTS.md`
- 중앙 문서: [`RFVisualizer-Docs`](https://github.com/2026-PNU-CSE-GRAD-RFVisualizer/RFVisualizer-Docs) (권장 로컬 위치 `../RFVisualizer-Docs`)
  - `PROJECT.md` — 목표, 파트 책임
  - `CURRENT_STATUS.md` — 구현 상태, 실시간 모드
  - `INTERFACE.md` — MQTT·RSSI·좌표·Export 계약
  - `network/NETWORK.md` — 백엔드 구조, 실험 모델
- 측정 절차·테스트 상세: 이 저장소의 `TESTING.md`

## 설치 (한 번만)

Python 3.11+ 권장. 저장소 폴더에서:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1        # (cmd: .\.venv\Scripts\activate.bat)
pip install -r requirements.txt
Copy-Item .env.example .env         # 없으면 자동 생성됨
```

`.env`의 `MQTT_HOST`는 개발 중 `127.0.0.1`. ESP32에서 접속할 땐 노트북 IP(`ipconfig`로 확인).

## 실행

### 1. MQTT 브로커 — A 또는 B 택1

```powershell
# A. Docker (PostgreSQL 포함)
docker compose up -d

# B. 개발용 브로커만 (DB 없음)
.\run_dev_mqtt_broker.ps1
```

### 2. 백엔드 (새 터미널)

```powershell
.\.venv\Scripts\Activate.ps1
.\run_backend.ps1
```

정상 확인: `http://127.0.0.1:8000/` (측정 페이지), `http://127.0.0.1:8000/health` (`enable_realtime: false`).

## 엔드포인트

| 경로 | 설명 |
|---|---|
| `GET /`                            | 강의실 측정 페이지(HTML) |
| `GET /monitor`                     | 노드 상태·성능 모니터(HTML) |
| `GET /health`                      | 상태/설정 |
| `GET /nodes/status`                | 노드별 online/전송빈도/손실 |
| `GET /metrics`                     | 수집지연·드롭률 |
| `POST /experiment/start` `/end`    | 실험 시작·종료 |
| `POST /experiment/assign`          | 노드 역할 배정(calibration/test) |
| `POST /experiment/tx`              | AP(TX) 좌표 등록 |
| `POST /offset-run/start` `/stop`   | 사전·사후 Offset 측정 (`{phase:"pre"\|"post"}`) |
| `POST /experiment/offsets/compute` | 장치 편차 계산 |
| `POST /run/start` `/end` · `GET /run/current` | 본 실험 Run(정/역방향) |
| `POST /test-segment/prepare` `/stop` `/discard` | T 위치 안정화→기록 구간 |
| `POST /run/attach-post-offset`     | 사후 Offset 을 Run 에 연결(drift) |
| `POST /experiment/points/import`   | 좌표 CSV 일괄 등록 |
| `POST /experiment/export`          | CSV 내보내기 + 품질 점검 |
| `GET /experiment/download/{which}` | raw / summary / calibration / test |

> 구 `POST /session/start` `/stop`, `GET /session/current` 는 **제거되어 HTTP 410 Gone** 을 반환한다.
> 자세한 흐름은 `RUN_EXPERIMENT.md` 참고.

`ENABLE_REALTIME=true` 일 때만 `GET /position/latest`, `WS /frames` 가 추가된다.

## 저장

| 저장소 | 위치 | 역할 |
|---|---|---|
| JSONL 원본 | `data/ingest_raw.jsonl` | 수신한 모든 메시지. 파싱 실패해도 남는다 |
| SQLite | `data/experiment.db` | 실험 데이터의 기준. 파일 하나를 그대로 백업 |
| Postgres | (선택) | 실시간 프레임 경로. 실험 결과의 기준이 아님 |

## Export 산출물 (`experiments/<experiment_id>/`)

```
raw/measurements_raw.csv              전체 시계열 (비정상 값도 valid=0 으로 보존)
raw/by_role/measurements_raw_*.csv    role별 원본 분리 (offset/calibration/test)
processed/measurements_summary.csv    위치·노드별 대표값
processed/offset_points.csv           장치 편차용
processed/calibration_points.csv      보정용 — IDW 입력
processed/test_points.csv             평가 전용
config/tx_rx.json                     TX/RX 좌표
config/device_offsets.json            장치별 보정값
qc_report.json                        품질 점검
README.md                             그래픽스 파트용 설명
```

그래픽스 파트는 `corrected_rssi` 열을 쓴다 (= Filtered RSSI 중앙값 + `device_offset_db`).
Calibration과 Test 데이터는 섞지 않는다.

## 테스트

```powershell
py -m pytest tests/test_experiment_pipeline.py -q   # 종단 검증 (브로커·DB 불필요)
py -m pytest -q                                     # 전체 단위 테스트
```

브로커가 떠 있으면 실제 MQTT 경로까지 포함한 리허설:

```powershell
python .\rehearsal.py --seconds 2   # 압축 실행
python .\rehearsal.py               # 30초 × 20위치, 실제와 동일
```

측정 절차와 단계별 테스트 상세는 `TESTING.md` 참조.

## 방화벽 (ESP32/타 PC 접속 시, 관리자 PowerShell)

```powershell
New-NetFirewallRule -DisplayName "JHKang MQTT 1883" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 1883
New-NetFirewallRule -DisplayName "JHKang Backend 8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000
```
