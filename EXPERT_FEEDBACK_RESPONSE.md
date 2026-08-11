# 전문가 자문 반영 (2026-08-01, 서준오 연구원)

중간보고서 자문의견서의 지적 3가지에 대한 네트워크/백엔드 대응 정리.

## ① 각 노드의 timestamp를 센서 값과 함께 저장 — 이미 반영됨

> "gateway의 시간이 아닌 각 노드의 timestamp를 센서 값과 함께 저장해야 합니다."

자문이 검토한 중간보고서는 gateway 시각만 쓰던 **구 실시간 시스템**이다.
논문용으로 재설계한 측정 경로는 이미 노드 시각과 서버 수신 시각을 **별도 컬럼으로 분리 저장**한다.

- `db/schema_experiment.sql`: `node_ts_ms`(노드 자기 시각) + `server_ts_ms`(서버 수신) 둘 다 보존
- Export `raw/measurements_raw.csv`: `timestamp`(노드) 와 `server_ts_ms`(서버) 두 열 모두 포함
- 실시간 프레임(`rssi_frame.nodes[].node_ts`, `server_receive_ms`)도 두 시각을 함께 전달

→ **추가 코드 변경 없음.** 재설계에서 선반영된 항목.

## ② 측정 시각 기준 윈도우 + 늦은 도착 유예 — 반영함

> "수집 지연 p95가 244ms로 200ms 동기화 윈도우를 이미 넘어 도착 시각 기준 묶음이
> 측정 시점과 어긋나고 있으니, 윈도우를 측정 시각 기준으로 바꾸고 늦은 도착을
> 기다릴 유예 구간을 두신다면…"

`backend/realtime/window.py`(실시간/졸업작품 경로)를 수정했다.

- 버킷 기준을 **서버 도착 시각 → 노드 측정 시각(`timestamp`)** 으로 변경.
  늦게 도착해도 측정 시각이 같은 구간이면 같은 프레임에 묶인다.
- **유예(grace) 구간** 추가: 구간 끝 이후에도 `window_grace_ms` 동안 늦은 도착을 기다린 뒤 확정.
  기본값 300ms (자문 시 p95 244ms를 여유 있게 포함).
- 확정 기준은 **측정시각 워터마크**(클럭 오프셋에 무관) + 스트림 정지 시 **서버 시계 fallback**
  으로 버킷이 영원히 막히지 않게 함.
- 유예까지 지나 도착한 샘플은 `late_dropped` 로 카운트(진단용).

검증: `tests/test_window.py` 7건 (측정시각 버킷팅, 유예 내 포함, 유예 후 폐기,
워터마크 확정, 정지 fallback, 최신값 유지, missing 노드) 전부 통과.

## ③ 30분+ 연속 시험으로 장시간 장애 확인 — 도구 추가

> "708건은 5개 소스가 1Hz로 보낸 약 2분 20초 분량이라 손실률 추세나 장시간 장애가
> 드러날 수 없는 구간… 30분 이상 연속 시험에서 순번 손실과 재연결 시간, heap 잔량을 기록…"

`endurance_monitor.py`(독립 실행 도구, 표준 라이브러리만) 추가.
실행 중인 백엔드의 `/metrics`·`/nodes/status`·`/health` 를 주기 폴링해 시계열 CSV를 남긴다.

기록 항목: 순번 손실 누계(`total_lost_packets`), MQTT 재연결(`mqtt_disconnect_count`),
수집지연 p95/max, 드롭률, 노드 online 수, 메시지 레이트.

```powershell
# 브로커·백엔드·부하 시뮬레이터를 띄운 뒤 함께 30분 실행
python load_test.py --nodes 5 --rate 1 --duration 1800
python endurance_monitor.py --duration 1800 --interval 10
```

검증: `tests/test_endurance_monitor.py` 4건 (집계 로직, 실패 행 처리, mock 서버 폴링,
서버 다운 시 기록) 전부 통과.

> heap 잔량은 ESP32(임베디드 파트) 몫이라 백엔드에서 관측 불가. 이 도구는 백엔드 쪽
> 장시간 지표(손실률 추세·재연결·지연 발산)를 담당한다. heap은 임베디드에 요청 필요.

---

## 적용 가이드 (파일 배치)

작업 폴더: `C:\RFVisualizer-Workspace\Network-Backend-Article`

아래 파일들을 같은 상대 경로에 그대로 덮어쓰기/추가한다. (main.py 는 2줄 수정을
미리 반영해 두었으므로 그대로 덮어쓰면 된다 — 수동 편집 불필요.)

| 파일 | 위치 | 방식 |
|---|---|---|
| `main.py` | `backend/main.py` | 덮어쓰기 (grace 배선 반영됨) |
| `config.py` | `backend/config.py` | 덮어쓰기 (`window_grace_ms=300` 추가) |
| `window.py` | `backend/realtime/window.py` | 덮어쓰기 |
| `endurance_monitor.py` | 저장소 루트 | 신규 |
| `test_window.py` | `tests/` | 신규 |
| `test_endurance_monitor.py` | `tests/` | 신규 |

참고 — main.py 에 반영된 2줄:
- `WindowBuffer(settings.window_size_ms)` → `WindowBuffer(settings.window_size_ms, settings.window_grace_ms)`
- `/health` 에 `"window_grace_ms": settings.window_grace_ms,` 추가

### 검증

```powershell
py tests\test_window.py               # 7/7 passed
py tests\test_endurance_monitor.py    # 4/4 passed
py -m pytest tests\ -q                 # 기존 테스트 포함 전체
```

## 인터페이스 영향

`rssi_frame` 의미가 바뀌었다(도착시각 → 측정시각 버킷, `grace_ms` 필드 추가).
실시간 경로(`ENABLE_REALTIME=true`)에서만 유효하며 논문 측정 경로에는 영향 없음.
정식 확정 시 `INTERFACE.md §9`(WebSocket Frame)에 반영 + 그래픽스 합의 필요(§13 절차).
