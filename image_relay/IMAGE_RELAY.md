# 이미지 중계 서버 (image_relay)

그래픽스가 렌더한 JPEG 프레임을 TCP로 받아 핸드헬드/뷰어에 중계한다.
INTERFACE.md §12 "JPEG Frame Streaming" 초안의 구체 구현. 측정 백엔드와 **별개 프로세스**.

```
Graphics ─TCP(ingest 9101)─▶ [ image_relay ] ─TCP(viewer 9102)─▶ Handheld / Viewer(들)
```

표준 라이브러리만 사용(외부 의존성 없음). 테스트용 `fake_producer`만 Pillow가 있으면 실제 JPEG 생성.

## 실행

```powershell
python -m image_relay --ingest-port 9101 --viewer-port 9102
```

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--host` | `0.0.0.0` | 바인드 주소 |
| `--ingest-port` | `9101` | 그래픽스가 프레임을 **보내는** 포트 |
| `--viewer-port` | `9102` | 뷰어/핸드헬드가 프레임을 **받는** 포트 |
| `--stats-interval` | `5` | 통계 로그 주기(초) |

## 자체 테스트 (그래픽스·임베디드 없이)

단위·종단 테스트:
```powershell
python image_relay\test_image_relay.py       # 8/8 passed 확인 (pytest 없이 실행 가능)
```

라이브 중계 (터미널 3개, 순서대로):
```powershell
python -m image_relay                                    # 1) 서버 (먼저, 계속 실행)
python -m image_relay.fake_viewer --save-dir frames_out  # 2) 뷰어
python -m image_relay.fake_producer --fps 10 --count 40  # 3) 가짜 그래픽스
```
→ `frames_out\`에 이미지가 쌓이면 성공.

## 방화벽 (다른 기기 접속 시, 관리자 PowerShell)

```powershell
New-NetFirewallRule -DisplayName "RFViz Relay ingest 9101" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 9101
New-NetFirewallRule -DisplayName "RFViz Relay viewer 9102" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 9102
```

## 와이어 포맷 (§12 미정 항목 확정본)

producer→server→viewer 동일. 정수는 **big-endian**.

```
22B 고정 헤더:
  magic   uint32  0x52464A46 ('RFJF')   프레임 시작 식별
  version uint8   1
  flags   uint8   0 (예약)
  seq     uint32  producer가 매 프레임 +1
  ts_ms   uint64  Unix Epoch millisecond
  length  uint32  뒤따르는 JPEG payload 바이트 수
payload   bytes   length 만큼의 JPEG
```

- `length` 상한 8 MB. 헤더가 규격과 다르면 서버는 그 연결을 끊는다(재접속 복구).
- 수신 측 참고 구현: `protocol.py`의 `read_frame()` (TCP 조각 대비 N바이트 채워 읽기 포함).

## 보장

- **최신 프레임 우선**: 뷰어별 큐 1칸 → 느린 뷰어는 오래된 프레임을 버리고 최신을 받는다.
- **격리**: 멈춘/느린 뷰어가 producer나 다른 뷰어를 막지 않는다 (검증됨).
- **메모리 상한·재접속**: 뷰어당 1프레임 + OS 버퍼로 제한, producer/viewer 재접속 자유.
- 뷰어가 소스 fps 이상이면 지연 ~1ms. 느리면 폐기로 지연 발산을 막는다(순간 지연 0은 아님).

## 그래픽스 파트 연동 규격

1. `TCP <서버IP>:9101` 접속
2. 프레임마다 위 22B 헤더 + JPEG payload 전송 (`seq` +1, `ts_ms` 생성 시각)
3. 해상도 **800×480** (임베디드 패널). 인코드 예시는 `protocol.encode_frame()`.

> 이 포맷은 §12 미정 항목을 이 구현 기준으로 채운 것이다. 파트 간 공식 확정은
> INTERFACE.md §12 갱신 + 그래픽스·임베디드 합의(§13 절차)가 필요하다.
