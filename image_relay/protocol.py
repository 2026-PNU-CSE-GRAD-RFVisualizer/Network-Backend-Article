"""JPEG 프레임 와이어 포맷 (INTERFACE.md §12 구체화).

포맷 (producer→server, server→viewer 동일, big-endian):
    22B 헤더 = magic(u32 'RFJF') version(u8) flags(u8) seq(u32) ts_ms(u64) length(u32)
    payload  = length 바이트의 JPEG (서버는 내용 해석 없이 바이트만 중계)
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from typing import Callable

MAGIC = 0x52464A46  # 'RFJF'
VERSION = 1

_HEADER = struct.Struct(">IBBIQI")  # magic, version, flags, seq, ts_ms, length
HEADER_SIZE = _HEADER.size  # 22
MAX_FRAME_BYTES = 8 * 1024 * 1024  # 손상 헤더의 과대 length 로부터 메모리 보호


class ProtocolError(Exception):
    """헤더 규격 위반. 연결을 끊고 재연결로 복구한다."""


@dataclass
class Frame:
    seq: int
    ts_ms: int
    payload: bytes
    flags: int = 0

    @property
    def raw(self) -> bytes:
        """헤더+payload 전체 와이어 바이트 (중계 시 그대로 재전송)."""
        return _HEADER.pack(
            MAGIC, VERSION, self.flags, self.seq, self.ts_ms, len(self.payload)
        ) + self.payload


def now_ms() -> int:
    return int(time.time() * 1000)


def encode_frame(payload: bytes, seq: int, ts_ms: int | None = None,
                 flags: int = 0) -> bytes:
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"payload {len(payload)}B 가 상한 {MAX_FRAME_BYTES}B 초과")
    return Frame(seq, ts_ms if ts_ms is not None else now_ms(), payload, flags).raw


def read_exactly(recv: Callable[[int], bytes], n: int) -> bytes:
    """정확히 n바이트 수신. TCP는 조각내 줄 수 있으므로 루프로 채운다. EOF면 b''."""
    chunks = bytearray()
    while len(chunks) < n:
        chunk = recv(n - len(chunks))
        if not chunk:
            return b""
        chunks.extend(chunk)
    return bytes(chunks)


def read_frame(recv: Callable[[int], bytes]) -> Frame | None:
    """프레임 하나 수신. 정상 종료(EOF)면 None, 규격 위반이면 ProtocolError."""
    header = read_exactly(recv, HEADER_SIZE)
    if not header:
        return None
    magic, version, flags, seq, ts_ms, length = _HEADER.unpack(header)
    if magic != MAGIC:
        raise ProtocolError(f"magic 불일치: 0x{magic:08X}")
    if version != VERSION:
        raise ProtocolError(f"version 불일치: {version} (기대 {VERSION})")
    if length > MAX_FRAME_BYTES:
        raise ProtocolError(f"length {length}B 가 상한 {MAX_FRAME_BYTES}B 초과")
    payload = read_exactly(recv, length) if length else b""
    if length and not payload:
        return None  # payload 도중 끊김
    return Frame(seq=seq, ts_ms=ts_ms, payload=payload, flags=flags)
