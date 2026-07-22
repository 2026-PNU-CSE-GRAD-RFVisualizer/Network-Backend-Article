# 3DGS RSSI Network Backend

RFVisualizer 프로젝트의 **네트워크/백엔드 파트**.
여러 ESP32 노드의 RSSI를 MQTT로 수집 → 검증 → 저장 → 분석용 CSV로 내보낸다.

## 현재 모드: 논문 실험 (7/23 강의실 측정)

이 저장소는 **논문 실험을 기준으로 정리되어 있다.**
9월 졸업작품용 실시간 시각화 기능은 `backend/realtime/` 에 격리되어 있고
`ENABLE_REALTIME=false` 로 꺼져 있다. 논문 제출 후 플래그만 켜면 복구된다.

| | 논문 실험 (지금) | 졸업작품 (9월) |
|---|---|---|
| 측정 방식 | 위치당 30초 정지 | 이동 단말 실시간 추적 |
| 저장 | SQLite + JSONL (Postgres 선택) | PostgreSQL |
| 전달 | CSV Export | WebSocket 프레임 push |
| 동기화 | 불필요 | 200ms Time Window |
| 기본 화면 | `/` 측정 페이지 | `/monitor` 대시보드 |

## 구현 항목

**논문 실험 경로 (활성)**

- 실험/세션 단위 측정 제어, 노드-위치 배치 모델
- Raw·Filtered RSSI 동시 보존, 비정상 값은 `valid=0` 으로 보존
- 장치별 RSSI 편차 자동 계산·보정
- CSV Export 4종 + 품질 점검 리포트
- JSONL 원본 이중 저장(비상 경로), SQLite 기준 저장소
- 현장 측정 웹페이지(`GET /`), 노드 상태 모니터(`GET /monitor`)

**공통**

- MQTT 구독(`rssi/#`, `gateway/#`, `status/+/lwt`), JSON 검증, timestamp skew 보정
- `seq` 기반 패킷 손실 카운트, LWT + heartbeat timeout 기반 online/offline
- 성능 지표(`/metrics`), 다중 노드 시뮬레이터(`load_test.py`), pytest

**실시간 경로 (`backend/realtime/`, 기본 비활성)**

- 200ms Time Window 프레임 동기화 + 결측 노드 표시
- WebSocket 프레임 push(WS `/frames`), `frame` 테이블 저장
- PositionEstimate 인터페이스(`/position/latest`)

## 0. 사전 준비 (한 번만)

Python 3.11+ 권장. 프로젝트 폴더에서:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # (cmd: .\.venv\Scripts\activate.bat)
pip install -r requirements.txt
Copy-Item .env.example .env         # 없으면 자동 생성됨
```

> `.env`의 `MQTT_HOST`는 개발 중 `127.0.0.1`. ESP32에서 접속할 땐 노트북 IP를 사용한다.
> 현재 노트북 실측 IP는 `ipconfig`로 확인 (이전 확인값 예: 192.168.0.107).

## 1. 인프라 실행 (MQTT + PostgreSQL) — 아래 A 또는 B 택1

### A. Docker (권장, PostgreSQL까지 포함)
Docker Desktop 실행 후:
```powershell
docker compose up -d
docker compose ps          # mosquitto, postgres 가 Up 인지 확인
```

### B. Docker 없이 (개발용 MQTT 브로커만, PostgreSQL 없음)
```powershell
.\run_dev_mqtt_broker.ps1
```
이 경우 DB 저장은 생략되고(무저장 모드) 실시간 파이프라인은 정상 동작한다.

## 2. 백엔드 실행 (새 터미널)

```powershell
.\.venv\Scripts\Activate.ps1
.\run_backend.ps1
```

정상 확인:
- 브라우저 `http://127.0.0.1:8000/`  → **강의실 측정 페이지**
- `http://127.0.0.1:8000/health`     → `enable_realtime: false` 확인 (논문 모드)

엔드포인트:

| 경로 | 설명 |
|---|---|
| `GET /`                            | 강의실 측정 페이지(HTML) |
| `GET /monitor`                     | 노드 상태·성능 모니터(HTML) |
| `GET /health`                      | 상태/설정 |
| `GET /nodes/status`                | 노드별 online/전송빈도/손실 |
| `GET /metrics`                     | 수집지연·드롭률 |
| `POST /experiment/start` `/end`    | 실험 시작·종료 |
| `POST /experiment/assign`          | 노드를 위치에 배치 |
| `POST /experiment/tx`              | AP(TX) 좌표 등록 |
| `POST /session/start` `/stop`      | 30초 측정 세션 |
| `GET /session/current`             | 진행 상황(측정 페이지가 폴링) |
| `POST /experiment/points/import`   | 좌표 CSV 일괄 등록 |
| `POST /experiment/offsets/compute` | 장치 편차 계산 |
| `POST /experiment/export`          | CSV 내보내기 + 품질 점검 |
| `GET /experiment/download/{which}` | raw / summary / calibration / test |

`ENABLE_REALTIME=true` 일 때만 추가로 붙는 경로:

| 경로 | 설명 |
|---|---|
| `GET /position/latest`| PositionEstimate 인터페이스(알고리즘 보류) |
| `WS  /frames`         | 동기화 프레임 push (접속 시 노드 스냅샷 1회) |

## 3. 테스트 (새 터미널, venv 활성화)

단일 publish:
```powershell
python .\test_publish.py
```
End-to-End 스모크 테스트(브로커+백엔드가 떠 있어야 함):
```powershell
python .\smoke_test.py
```
다중 노드 시뮬레이터(대시보드에서 실시간 확인):
```powershell
python .\load_test.py --nodes 4 --rate 5 --duration 30 --loss 0.05
```
단위 테스트(순수 로직):
```powershell
pytest -q
```

## 4. 방화벽 (ESP32/타 PC에서 접속 시, 관리자 PowerShell)

```powershell
New-NetFirewallRule -DisplayName "JHKang MQTT 1883" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 1883
New-NetFirewallRule -DisplayName "JHKang Backend 8000" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000
```

## 5. ESP32 펌웨어 측 합의사항

- 브로커 주소 = 노트북 IP (같은 서브넷), 포트 1883
- 발행 토픽 `rssi/<node_id>`, 상태 `status/<node_id>/lwt` (LWT retained)
- 메시지 스키마: `node_id, timestamp(ms), ap_bssid, rssi, rssi_raw, seq, status`
  - `seq`는 손실 측정에 필수(손실 시에도 증가)

---

# 논문 실험 (7/23 강의실 측정)

실시간 파이프라인과 **독립적으로** 동작하는 실험 경로가 추가되어 있다.
Postgres/Docker 가 없어도 측정이 가능하다.

## 저장 구조

| 저장소 | 위치 | 역할 |
|---|---|---|
| JSONL 원본 | `data/ingest_raw.jsonl` | 수신한 모든 메시지. 파싱 실패해도 남는다 |
| SQLite | `data/experiment.db` | 실험 데이터의 기준. 파일 하나를 그대로 백업 |
| Postgres | (선택) | 기존 실시간 프레임 경로. 실험 결과의 기준이 아님 |

## 데이터 모델의 핵심

**샘플의 위치는 세션 라벨이 아니라 그 노드의 배치로 결정된다.**

고정 보정 센서 4대는 실험 내내 각자의 위치에 있고 이동 센서 1대만 옮겨 다니므로,
"세션 1개 = 위치 1개" 모델은 물리적으로 틀리다. 노드를 위치에 배치(`/experiment/assign`)해
두면 모든 세션 동안 각 노드의 데이터가 자기 위치에 쌓인다.
그 결과 보정점은 30개가 아니라 수백 개의 샘플을 얻는다.

## 현장 절차 (`http://127.0.0.1:8000/measure`)

```
1. 실험 시작          experiment_id / BSSID / 채널 입력
2. AP 좌표 등록       POST /experiment/tx
3. 장치 편차 측정      5대를 한 책상에 모음 → [전체를 offset-00 으로] → 30초 측정
4. 고정 센서 배치      node1→cal-01, node2→cal-02, node3→cal-03, node5→cal-04
5. 안정화 측정        point_id=cal-01 로 30초
6. Test 15개         이동 센서 node_id 지정 → point_id 입력 → Enter → 30초 자동 종료
                     (끝나면 point_id 가 test-02, test-03... 으로 자동 증가)
7. 좌표 등록          종이에 적어온 값을 points.csv 형식으로 붙여넣기
8. CSV 내보내기       품질 점검 결과 확인 → 문제 0건이면 완료
```

측정을 망쳤으면 **[버리고 재측정]** 을 누른다. 이전 세션만 폐기되고
같은 시간에 흘러든 보정 센서 데이터는 보존된다.

## 산출물 (`experiments/<experiment_id>/`)

```
raw/measurements_raw.csv           전체 시계열 (비정상 값도 valid=0 으로 보존)
processed/measurements_summary.csv 위치·노드별 대표값
processed/calibration_points.csv   보정 4개 — IDW 입력
processed/test_points.csv          Test 15개 — 평가 전용
config/tx_rx.json                  TX/RX 좌표
config/device_offsets.json         장치별 보정값
qc_report.json                     품질 점검
README.md                          그래픽스 파트용 설명
```

그래픽스 파트는 `corrected_rssi` 열을 쓴다.
= Filtered RSSI 30초 중앙값 + `device_offset_db`

## 검증

```powershell
python .\tests\test_experiment_pipeline.py     # pytest 없이 실행 가능
```

가상 노드에 알려진 장치 편차를 심어 두고 백엔드가 그 값을 복원하는지 확인한다.
7/23 절차 전체(20개 위치)를 재현하고 CSV 산출물까지 검사한다.

브로커가 있으면 실제 MQTT 경로까지 포함한 리허설을 돌릴 수 있다:

```powershell
python .\rehearsal.py --seconds 2      # 압축 실행
python .\rehearsal.py                  # 30초 × 20위치, 실제와 동일
```

## 펌웨어 합의 필요 사항

| 항목 | 백엔드 처리 |
|---|---|
| Filtered RSSI | `rssi` \| `rssi_filtered` \| `rssi_filtered_dbm` 중 아무 이름이나 인식 |
| Raw RSSI | `rssi_raw` \| `rssi_raw_dbm` |
| ×10 정수 전송 | `.env` 의 `RSSI_FILTERED_SCALE=10` 으로 대응 |
| 채널 | `ap_channel` \| `channel` |
| 오류 플래그 | `error_flags` \| `status`. 0 이 아니면 valid=0 으로 기록 |

**확인 필요:** 나영이 쪽 펌웨어가 `rssi` 를 Filtered 로 보내는지 Raw 로 보내는지.
현재는 `rssi` = Filtered 로 가정한다. 반대면 대표값이 통째로 틀어진다.

## 프로젝트 구조

```
backend/
  config.py        설정 (ENABLE_REALTIME 플래그 포함)
  parsing.py       ESP32 페이로드 파싱 — 외부 의존성 없음, 단위 테스트 가능
  store.py         SQLite + JSONL 실험 저장소            ← 논문
  experiment.py    세션 제어, 노드 배치, 장치 offset 계산  ← 논문
  export.py        CSV Export, 좌표 등록, 품질 점검       ← 논문
  pages.py         현장 측정 웹페이지                    ← 논문
  state.py         NodeRegistry (노드 online/손실)
  database.py      PostgreSQL (선택)
  metrics.py       성능 지표
  mqtt_bridge.py   MQTT 수신 → 검증 → 저장
  main.py          FastAPI 앱
  realtime/        200ms Window, WS, frame, PositionEstimate  ← 졸업작품(기본 꺼짐)
db/
  schema_experiment.sql  SQLite 실험 스키마
  schema.sql             PostgreSQL 스키마
tests/
  test_experiment_pipeline.py  종단 검증 (브로커·DB 불필요)
  test_state.py / test_metrics.py
rehearsal.py     실제 MQTT 경로까지 포함한 현장 리허설
load_test.py     다중 노드 시뮬레이터
```

## 논문 제출 후: 졸업작품 모드로 복귀

```powershell
# .env 에서
ENABLE_REALTIME=true
```

이것만으로 200ms Window 동기화, WS `/frames`, `frame` 테이블 저장,
`/position/latest` 가 다시 켜진다. 코드 변경은 필요 없다.

이후 졸업작품에서 이어서 할 일은 논문 실험 범위 밖으로 남겨둔 항목들이다:
실시간 3D 히트맵, SIBR Viewer 통합, 3DGS 영상과 RF 결과 합성,
이동 단말 실시간 위치 추적, IMU 연동, 장시간 안정성 시험.

## 인증(배포 전)
개발 중 `mosquitto.conf`는 `allow_anonymous true`. 시연/배포 시 계정 인증으로 전환하고
`.env`의 `MQTT_USERNAME/MQTT_PASSWORD`를 설정한다.
