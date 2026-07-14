# 3DGS RSSI Network Backend

착수보고서 및 `3DGS_Sionna_RT_실시간_시각화_파이프라인.md`의 **네트워크/백엔드 파트** 구현.
여러 ESP32 노드의 RSSI를 MQTT로 수집 → 검증/필터링 → 200ms Time Window 동기화 →
PostgreSQL 저장 + WebSocket으로 그래픽스 파트에 프레임 전달, 노드 상태·성능 지표를 관측한다.

## 구현 항목

- MQTT 구독(`rssi/#`, `status/+/lwt`), JSON 검증·RSSI 범위 필터, timestamp skew 보정
- `seq` 기반 패킷 손실 카운트, LWT + heartbeat timeout 기반 노드 online/offline
- 200ms Time Window 프레임 동기화 + 결측 노드 표시
- PostgreSQL 저장(원시값 **배치 삽입** / 프레임 / 노드상태), 무 DB 시 graceful degrade
- 성능 지표: 수집지연, 프레임 생성시간, 드롭률 (`/metrics`)
- 실시간 모니터링 대시보드(`GET /`), 다중 노드 시뮬레이터(`load_test.py`), pytest

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
- 브라우저 `http://127.0.0.1:8000/`  → 실시간 모니터링 대시보드
- `http://127.0.0.1:8000/health`     → `postgres_connected` 값 확인 (B안이면 false 정상)

엔드포인트:
| 경로 | 설명 |
|---|---|
| `GET /`               | 모니터링 대시보드(HTML) |
| `GET /health`         | 상태/설정 |
| `GET /nodes/status`   | 노드별 online/전송빈도/손실 |
| `GET /metrics`        | 수집지연·프레임생성·드롭률 |
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

## 프로젝트 구조

```
backend/       config, database(배치삽입), state, metrics, mqtt_bridge, ws, main
db/schema.sql  PostgreSQL 스키마
mosquitto/     mosquitto.conf
tests/         pytest (state, metrics)
dev_mqtt_broker.py  Docker 없이 쓰는 개발 브로커(amqtt)
load_test.py   다중 노드 시뮬레이터
test_publish.py / smoke_test.py  수동 테스트
docker-compose.yml
```

## 인증(배포 전)
개발 중 `mosquitto.conf`는 `allow_anonymous true`. 시연/배포 시 계정 인증으로 전환하고
`.env`의 `MQTT_USERNAME/MQTT_PASSWORD`를 설정한다.
