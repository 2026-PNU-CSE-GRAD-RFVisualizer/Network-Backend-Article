"""ESP32 페이로드 파싱 — 외부 의존성 없음, 브로커 없이 단위 테스트 가능."""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)


class ParseConfig(NamedTuple):
    rssi_min: float = -100.0
    rssi_max: float = -10.0
    timestamp_max_skew_ms: int = 600_000
    rssi_filtered_scale: float = 1.0


def parse_measurement(data: dict[str, Any], receive_ms: int,
                      cfg: ParseConfig) -> dict[str, Any] | None:
    """페이로드 1건 파싱.

    None = 구조가 깨져 노드/시각도 알 수 없음(이때만 버림). 값이 이상한 경우는
    valid=False 로 통과시켜 Raw 는 보존하고 분석 단계에서만 제외한다(계획서 §7.2).
    """
    if not {"node_id", "timestamp"} <= data.keys():
        logger.warning("drop missing fields: %s", sorted({"node_id", "timestamp"} - set(data)))
        return None

    try:
        node_id = str(data["node_id"])
        timestamp = int(data["timestamp"])
        seq = int(data["seq"]) if data.get("seq") is not None else None
        error_flags = int(data.get("error_flags", data.get("status", 0)) or 0)
    except (TypeError, ValueError):
        logger.warning("drop invalid field types: %s", data)
        return None

    def _f(*keys: str) -> float | None:
        for key in keys:
            v = data.get(key)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        return None

    def _i(*keys: str) -> int | None:
        v = _f(*keys)
        return int(v) if v is not None else None

    # 펌웨어 필드명 호환: Filtered=rssi|rssi_filtered|rssi_filtered_dbm, Raw=rssi_raw|rssi_raw_dbm
    filtered = _f("rssi_filtered_dbm", "rssi_filtered", "rssi")
    raw = _f("rssi_raw_dbm", "rssi_raw")

    scale = cfg.rssi_filtered_scale
    if scale and scale != 1.0:
        if filtered is not None:
            filtered /= scale
        if raw is not None:
            raw /= scale

    invalid_reason: str | None = None
    if filtered is None:
        invalid_reason = "rssi_missing"
    elif not (cfg.rssi_min <= filtered <= cfg.rssi_max):
        invalid_reason = f"rssi_out_of_range({filtered})"
    elif error_flags != 0:
        invalid_reason = f"error_flags({error_flags})"

    if invalid_reason:
        logger.warning("invalid sample node=%s reason=%s", node_id, invalid_reason)

    if abs(receive_ms - timestamp) > cfg.timestamp_max_skew_ms:
        logger.warning("replace skewed node timestamp node=%s timestamp=%s", node_id, timestamp)
        timestamp = receive_ms

    return {
        "node_id": node_id,
        "timestamp": timestamp,
        "ap_bssid": data.get("ap_bssid"),
        "ap_channel": _i("ap_channel", "channel"),
        # 실시간 파이프라인은 정수 rssi 를 기대하므로 대표값을 rssi 로도 유지
        "rssi": int(round(filtered)) if filtered is not None else None,
        "rssi_filtered": filtered,
        "rssi_raw": raw,
        "sample_count": _i("sample_count"),
        "seq": seq,
        "status": error_flags,
        "valid": invalid_reason is None,
        "invalid_reason": invalid_reason,
        "pos_x": _f("pos_x"), "pos_y": _f("pos_y"), "pos_z": _f("pos_z"),
        "rot_w": _f("rot_w"), "rot_x": _f("rot_x"),
        "rot_y": _f("rot_y"), "rot_z": _f("rot_z"),
    }
