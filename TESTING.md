# 자체 테스트 순서

의존성이 적은 것부터 올라간다. **앞 단계가 통과해야 뒤 단계가 의미 있다.**
1단계에서 실패하면 브로커를 띄워도 소용없고, 3단계에서 실패하면 실물 ESP32 를 붙여도 소용없다.

| 단계 | 검증 대상 | 필요한 것 | 소요 | 개발환경 확인 |
|---|---|---|---|---|
| 0 | 환경 | Python | 5분 | — |
| 1 | 계산 로직 | 없음 | 10초 | 통과 (9/9) |
| 2 | 서버 기동 | 브로커 | 2분 | **미확인** |
| 3 | MQTT 종단 경로 | 브로커 + 백엔드 | 3분 | 통과 |
| 4 | 사람이 쓰는 UI | 위와 동일 | 10분 | **미확인** |
| 5 | 실물 ESP32 | 나영이 필요 | — | **미확인** |

"개발환경 확인" 은 이 코드를 작성한 환경에서의 결과다.
네 노트북에서 다시 돌려봐야 하고, **미확인 항목은 아무도 아직 해보지 않았다.**

---

## VS Code 통합 터미널에서 실행할 때

모든 명령은 VS Code 통합 터미널(`` Ctrl+` ``)에서 그대로 친다.
터미널이 3~4개 동시에 필요하다 — `Ctrl+Shift+5` 로 분할한다.

**터미널을 열 때마다 확인할 것**

```powershell
# 1. 작업 폴더가 Network_Backend 인가
pwd

# 2. venv 가 활성화되어 있는가 (프롬프트 앞에 (.venv) 표시)
.\.venv\Scripts\Activate.ps1
```

VS Code 가 venv 를 자동 활성화하기도 하지만, 새 터미널에서는 안 될 때가 있다.
`(.venv)` 표시가 없으면 위 명령을 친다.

**PowerShell 실행 정책 오류가 나면** (`이 시스템에서 스크립트를 실행할 수 없으므로`):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

현재 터미널에서만 풀리므로 안전하다.

**주의 — `uvicorn --reload` 를 쓰지 마라.**
측정 중에 코드 파일을 저장하면 백엔드가 재시작되고 그 위치 측정이 날아간다.
아래 명령에는 일부러 넣지 않았다.

---

## 명령어 한눈에

터미널별로 나눠서 정리했다. 각 단계의 자세한 통과 기준은 아래 본문에 있다.

```powershell
# ─── 공통: 모든 터미널에서 먼저 ───
cd <경로>\Network_Backend
.\.venv\Scripts\Activate.ps1

# ─── 0단계: 최초 1회만 ───
python -m venv .venv
pip install -r requirements.txt
Copy-Item .env.example .env

# ─── 1단계: 로직 테스트 (터미널 아무거나, 10초) ───
python .\tests\test_experiment_pipeline.py
python -m pytest -q

# ─── 2단계: 터미널 1 — 브로커 ───
python .\dev_mqtt_broker.py
#   (Docker 쓰면 대신) docker compose up -d

# ─── 2단계: 터미널 2 — 백엔드 ───
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000

# ─── 3단계: 터미널 3 — 리허설 ───
python .\rehearsal.py --seconds 2      # 압축 실행, 약 1분
python .\rehearsal.py                  # 실제 속도, 약 10분

# ─── 4단계: 터미널 3 — 가상 노드 5대를 켜 두고 브라우저 조작 ───
python .\load_test.py --nodes 5 --rate 1 --duration 900

# ─── 5단계: 터미널 3 — 실물 ESP32 페이로드 확인 ───
python .\sniff.py
```

브라우저: `http://127.0.0.1:8000/` (측정) / `http://127.0.0.1:8000/monitor` (노드 상태)

---

## 0단계 — 환경 준비 (한 번만)

```powershell
cd Network_Backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

`.env` 를 열어 확인:

```
ENABLE_REALTIME=false      # 논문 모드
RSSI_FILTERED_SCALE=1.0    # 펌웨어가 x10 으로 보내면 10
```

---

## 1단계 — 계산 로직 (브로커·DB·인터넷 전부 불필요)

```powershell
python .\tests\test_experiment_pipeline.py
```

7/23 절차 20개 위치를 통째로 재현하고, 가상 노드에 심어 둔 장치 편차를
백엔드가 복원하는지 확인한다.

**통과 기준**

```
PASS  test_calibration_nodes_accumulate_across_sessions
PASS  test_corrected_rssi_removes_device_bias
PASS  test_device_offsets_recover_known_bias
PASS  test_export_produces_graphics_ready_files
PASS  test_invalid_samples_preserved_not_dropped
PASS  test_qc_catches_missing_coordinates_and_mixed_bssid
PASS  test_remeasure_supersedes_only_that_point
PASS  test_test_points_are_measured_by_moving_node_only
PASS  test_x10_scale_and_field_aliases

모두 통과
```

기존 테스트도 같이:

```powershell
pytest -q
```

---

## 2단계 — 브로커와 백엔드 기동

**터미널 1 — 브로커** (아래 A 또는 B)

```powershell
# A. Docker 있으면
docker compose up -d

# B. Docker 없으면 (개발용 브로커, Postgres 없음)
python .\dev_mqtt_broker.py
```

**터미널 2 — 백엔드**

```powershell
.\.venv\Scripts\Activate.ps1
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

> `.ps1` 스크립트(`run_backend.ps1`, `run_dev_mqtt_broker.ps1`)를 써도 결과는 같다.
> 위 명령은 그 스크립트가 내부에서 실행하는 것과 동일하며,
> `.env` 가 없으면 `run_backend.ps1` 이 자동 생성해 주는 것만 다르다.

로그에 이 줄이 보여야 한다:

```
realtime pipeline disabled — 논문 실험 모드
experiment store ready: ...\data\experiment.db
mqtt connected: Success
```

**통과 기준** — 브라우저에서 `http://127.0.0.1:8000/health`

```json
{
  "ok": true,
  "enable_realtime": false,
  "experiment_id": null,
  "experiment_db": "...\\data\\experiment.db"
}
```

> `postgres_connected: false` 는 B안이면 정상이다. 논문 실험은 SQLite 를 쓴다.

---

## 3단계 — MQTT 종단 경로 (가상 ESP32 5대)

**터미널 3**

```powershell
.\.venv\Scripts\Activate.ps1
python .\rehearsal.py --seconds 2
```

실제 강의실 크기의 경로손실 모델로 가상 노드 5대가 MQTT 로 발행하고,
7/23 절차 전체를 HTTP API 로 실행한 뒤 CSV 까지 만든다.
**1단계와 달리 브로커·FastAPI·paho 를 실제로 통과한다.**

**통과 기준**

```
[5] 장치 offset 계산
  node        계산된 offset      참값      오차
  node1              -1.40     -1.80      0.40
  ...
  최대 오차 0.85 dB

[6] CSV 내보내기
  저장 위치: experiments\rehearsal_20260721_...
```

> `--seconds 2` 로 압축 실행하면 위치당 샘플이 2개뿐이라
> **"샘플 수 부족" 문제가 뜨는 게 정상이다.** 경로 검증이 목적이므로 무시해도 된다.
> 품질 점검까지 완전히 통과시켜 보려면 `.env` 에 `EXPECTED_SAMPLES_PER_POINT=2` 를
> 넣거나, `python .\rehearsal.py` 를 옵션 없이(30초 × 20위치, 약 10분) 돌린다.

**실패하면 여기서 멈춰라.** 나영이 펌웨어를 붙여도 똑같이 실패한다.

---

## 4단계 — 손으로 UI 조작 (7/23 예행연습)

여기가 제일 중요하다. **7/23 당일에 네가 실제로 할 동작을 미리 해보는 것.**

**터미널 3 — 가상 노드 5대를 계속 켜둔다**

```powershell
python .\load_test.py --nodes 5 --rate 1 --duration 900
```

**브라우저** — `http://127.0.0.1:8000/`

1. **실험 시작** — experiment_id `test_rehearsal`, BSSID 아무거나, 채널 6 → [실험 시작]
2. **AP 좌표 등록** — 1번 패널 아래 x/y/z 에 `7.7`, `1.5`, `1.2` 입력 → [AP 좌표 등록]
   → 옆 표시가 빨간 "미등록" 에서 초록 "등록됨" 으로 바뀌는지 확인
3. **장치 편차** — [전체를 offset-00 으로] → point_id `offset-00`, 역할 `offset` → [측정 시작]
   → 30초 카운트다운, 노드 5개가 표에 뜨고 유효 샘플이 올라가는지 확인
4. **고정 센서 배치** — node1→cal-01, node2→cal-02, node3→cal-03, node5→cal-04 (역할 calibration)
5. **안정화** — point_id `cal-01`, 역할 `calibration` → [측정 시작]
6. **Test 측정** — 이동 센서 `node4`, point_id `test-01`, 역할 `test` → Enter
   → 끝나면 point_id 가 `test-02` 로 자동 증가하는지 확인 → Enter 반복 (3~4개만 해봐도 충분)
7. **재측정 확인** — 아무 위치에서 [버리고 재측정] 눌러보고, 같은 point_id 로 다시 측정
8. **좌표 등록** — 텍스트 상자에 붙여넣고 [좌표 등록]
   ```
   point_id,point_role,pos_x,pos_y,pos_z
   offset-00,offset,7.0,5.0,0.80
   cal-01,calibration,2.4,2.0,0.78
   cal-02,calibration,12.8,2.6,0.85
   cal-03,calibration,3.1,8.4,1.32
   cal-04,calibration,13.0,9.2,1.40
   test-01,test,2.0,1.6,0.89
   test-02,test,4.85,1.6,0.89
   test-03,test,7.7,1.6,0.89
   ```
9. **offset 계산** → [장치 offset 계산]
10. **내보내기** → [CSV 내보내기]

**확인할 것**

- [ ] 측정 중 노드 5개가 전부 표에 뜨고 유효 샘플이 초당 1개씩 증가
- [ ] 30초 후 자동 종료되고 point_id 가 자동 증가
- [ ] 진행 상황에 측정 완료한 위치가 칩으로 쌓임
- [ ] `experiments\test_rehearsal\` 에 파일 생성
- [ ] `processed\test_points.csv` 를 Excel 로 열었을 때
      한글 안 깨지고 x/y/z 와 corrected_rssi 가 채워져 있음
- [ ] `processed\calibration_points.csv` 의 sample_count 가 30보다 훨씬 큼 (세션 간 누적)
- [ ] 품질 점검에서 좌표 등록 안 한 test 위치가 문제로 잡힘

**일부러 실패시켜 보기** (이게 진짜 테스트다)

- 좌표 등록을 건너뛰고 내보내기 → "좌표 미등록 위치" 가 잡히는가
- AP 좌표 등록을 건너뛰고 내보내기 → "TX(AP) 좌표가 등록되지 않았습니다" 가 잡히는가
- 측정 중 `load_test.py` 를 Ctrl+C 로 죽이기 → 노드가 OFFLINE 으로 바뀌는가
- 백엔드를 껐다 켜고 같은 experiment_id 로 실험 시작 → 노드 배치가 복원되는가

**측정 중 백엔드를 강제 종료해 보기** — `data\ingest_raw.jsonl` 을 메모장으로 열어
그때까지 받은 원본이 남아 있는지 확인. 이게 7/23 비상 경로다.

---

## 4.5단계 — 브로커 재시작 내성

**먼저 알아둘 것: 이건 "복구되니까 괜찮다" 를 확인하는 게 아니라,
"끊긴 동안의 데이터는 영구히 사라진다" 를 눈으로 보는 테스트다.**

MQTT QoS 0 이고 ESP32 는 버퍼가 없다. 브로커가 죽어 있는 동안 발행된 RSSI 는
어디에도 저장되지 않는다. 백엔드는 자동 재연결하고 구독도 다시 걸지만,
**끊긴 3초 동안의 3개 샘플은 복구할 방법이 없다.**

그래서 이 테스트의 목적은 두 가지다.

1. 재연결과 구독 재설정이 자동으로 되는가 (되어야 함)
2. **끊긴 사실을 네가 알아챌 수 있는가** (이게 더 중요)

### A. 측정을 안 하는 상태에서 재시작

브로커와 백엔드가 떠 있는 상태에서:

```powershell
# Docker
docker compose restart mosquitto

# 개발 브로커면 해당 터미널에서 Ctrl+C 후 다시 python .\dev_mqtt_broker.py
```

**백엔드 로그에 이 줄이 떠야 한다:**

```
WARNING ... mqtt disconnected: ...
WARNING ... mqtt RECONNECTED (2번째): Success — 구독 재설정 완료
```

`http://127.0.0.1:8000/health` 확인:

```json
"mqtt": {
  "connected": true,
  "connect_count": 2,
  "disconnect_count": 1
}
```

**여기서 `connect_count` 가 2로 올라가지 않으면 재구독이 안 된 것이다.**
연결은 살아 있는데 메시지가 안 들어오는, 제일 찾기 어려운 고장이다.

재시작 후 `load_test.py` 를 다시 돌려 데이터가 계속 들어오는지 확인한다.

### B. 측정 중에 재시작 (진짜 테스트)

1. `load_test.py --nodes 5 --rate 1 --duration 300` 실행
2. 측정 페이지에서 point_id `crash-test`, 역할 `test`, 측정 시간 `30` → [측정 시작]
3. **카운트다운 10초쯤에 브로커를 재시작한다**
4. 화면 최상단을 본다

**통과 기준**

- 끊긴 즉시 빨간 띠: `MQTT 브로커 연결 끊김 — 지금 측정하면 데이터가 저장되지 않습니다`
- 재연결되면 노란 띠: `MQTT 재연결됨 (끊김 1회)`
- 백엔드 로그에 `mqtt DISCONNECTED 측정 중! point=crash-test — 재측정이 필요할 수 있다`
- 노드별 유효 샘플이 30보다 확연히 적음 (끊긴 시간만큼 빠짐)
- 30초 후 세션은 정상 종료됨

이 상태에서 [버리고 재측정] 을 눌러 같은 point_id 로 다시 측정하면
정상 샘플 수로 복구되는지 확인한다.

### C. 백엔드만 재시작

측정 중이 아닐 때 백엔드를 Ctrl+C 후 다시 띄우고, 같은 experiment_id 로 [실험 시작].

**통과 기준**

- 노드 배치가 그대로 복원됨 (로그에 `... N assignments restored`)
- 이전에 측정한 위치들이 진행 상황에 그대로 남아 있음
- 이어서 측정하면 같은 SQLite 파일에 계속 쌓임

> 백엔드 재시작은 안전하다. SQLite 에 이미 커밋되어 있다.
> 브로커 재시작과 달리 데이터 손실이 없다.

### 7/23 현장에서의 의미

| 상황 | 결과 | 대응 |
|---|---|---|
| 브로커 죽음 | 죽은 동안 데이터 영구 손실 | 빨간 띠 보이면 즉시 중단, 복구 후 그 위치 재측정 |
| 백엔드 죽음 | 손실 없음 | 다시 띄우고 같은 experiment_id 로 이어서 |
| 노트북 죽음 | `data/` 폴더는 남음 | 재부팅 후 이어서. 측정 후 USB 백업 필수 |
| ESP32 1대 죽음 | 그 노드만 빠짐 | 해당 위치만 재측정 |

**측정 중에는 브로커 터미널을 건드리지 마라.** 실수로 Ctrl+C 하면
그 위치는 재측정해야 한다. 터미널을 최소화해 두는 게 안전하다.

---

## 5단계 — 실물 ESP32 (나영이와 함께)

여기서 처음 확인되는 것은 **펌웨어 필드명** 하나뿐이다. 나머지는 4단계에서 다 검증됐다.

```powershell
# 백엔드를 띄운 채로, 실제로 뭐가 오는지 눈으로 본다
python .\sniff.py
```

**반드시 확인할 것**

1. `rssi` 가 Filtered 인가 Raw 인가 — 지금 백엔드는 `rssi` = Filtered 로 가정한다
2. 값의 크기 — `-553` 처럼 오면 x10 이므로 `.env` 에 `RSSI_FILTERED_SCALE=10`
3. `ap_channel` 이 들어오는가
4. `seq` 가 손실 시에도 증가하는가

`.env` 를 고쳤으면 백엔드를 재시작하고 3단계부터 다시 돌린다.

---

## 고장 진단표

**아래는 테스트 결과가 아니라, 실패했을 때 어디를 봐야 하는지 찾는 표다.**

| 실패한 단계 | 원인이 있는 곳 | 대응 |
|---|---|---|
| 1단계 | 계산 로직 | 브로커를 띄워도 소용없다. 코드 문제 |
| 2단계 | 포트·방화벽·패키지 설치 | 1883 / 8000 포트 점유, venv 활성화 확인 |
| 3단계 | MQTT 경로 | 실물 ESP32 를 붙여도 똑같이 실패한다 |
| 4단계 | 현장 절차·UI | 7/23 당일에 겪을 일을 미리 겪은 것. 이득 |
| 5단계 | 펌웨어 스키마 불일치 | 대부분 `.env` 의 필드명·스케일로 해결 |

## 현재까지 확인된 것

작성 시점(7/21) 기준, 개발 환경에서 아래까지는 통과를 확인했다.
**네 노트북에서 다시 돌려봐야 하는 이유는, 아래가 내 환경 기준이기 때문이다.**

| 항목 | 상태 |
|---|---|
| `test_experiment_pipeline.py` | 9/9 통과 |
| `test_state.py` | 7/7 통과 |
| `test_metrics.py` | 3/3 통과 |
| 장치 offset 복원 오차 | 1 dB 미만 |
| 보정 후 장치 간 편차 | 5.6 dB → 1 dB 미만 |
| CSV 산출물 6종 + 품질 점검 | 생성 확인 |

아직 **한 번도 검증되지 않은 것**은 2·4·5단계다.
브로커와 FastAPI 를 실제로 띄우는 경로, 사람이 브라우저로 조작하는 경로,
그리고 실물 펌웨어의 필드명. 이 셋이 네가 지금 해볼 부분이다.
