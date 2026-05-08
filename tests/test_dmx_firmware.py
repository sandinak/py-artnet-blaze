"""Enttec USB DMX Pro firmware-query frame + response parsing."""

from __future__ import annotations

import logging
import struct
import threading
from collections import deque

from artnet_blaze.dmx import (
    ENTTEC_END,
    ENTTEC_LABEL_GET_PARAMS,
    ENTTEC_START,
    build_enttec_get_params_frame,
    parse_enttec_params_response,
    query_enttec_firmware,
)


def _quiet():
    log = logging.getLogger("dmx-fw")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


class _ScriptedSerial:
    """Serial fake with a programmable read script.

    Tests construct `_ScriptedSerial(read_script=[...])` where the script
    is a list of bytes-or-empty chunks that successive read() calls
    return. Writes are captured for assertion.
    """

    def __init__(self, read_script: list[bytes] | None = None) -> None:
        self.writes: list[bytes] = []
        self._reads: deque[bytes] = deque(read_script or [])
        self.flushes = 0
        self.input_resets = 0
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        self.flushes += 1

    def reset_input_buffer(self) -> None:
        self.input_resets += 1

    def read(self, n: int = 1) -> bytes:
        with self._lock:
            return self._reads.popleft() if self._reads else b""


def test_get_params_frame_layout():
    frame = build_enttec_get_params_frame()
    assert frame[0] == ENTTEC_START
    assert frame[1] == ENTTEC_LABEL_GET_PARAMS
    (length,) = struct.unpack("<H", frame[2:4])
    assert length == 2  # the user_size field
    assert frame[-1] == ENTTEC_END


def test_parse_response_extracts_firmware():
    # Build a synthetic 'Get Widget Parameters' reply:
    # start, label=3, len=5 (firmware-revision-only), fw_lsb, fw_msb,
    # break, mab, refresh, end
    reply = (
        bytes([ENTTEC_START, ENTTEC_LABEL_GET_PARAMS])
        + struct.pack("<H", 5)
        + bytes([0x42, 0x01, 9, 1, 40, ENTTEC_END])
    )
    parsed = parse_enttec_params_response(reply)
    assert parsed == "fw 1.66"  # 0x42 = 66 lsb, 0x01 = 1 msb


def test_parse_response_returns_none_on_garbage():
    assert parse_enttec_params_response(b"") is None
    assert parse_enttec_params_response(b"\x00\x01\x02") is None
    # No end byte
    assert parse_enttec_params_response(b"\x7e\x03\x05\x00\x42\x01\x09\x01\x28") is None


def test_query_enttec_firmware_round_trip():
    # Reply chunked across two reads (realistic for serial)
    reply_part1 = bytes([ENTTEC_START, ENTTEC_LABEL_GET_PARAMS, 0x05, 0x00])
    reply_part2 = bytes([0x10, 0x02, 9, 1, 40, ENTTEC_END])
    port = _ScriptedSerial(read_script=[reply_part1, b"", reply_part2])

    result = query_enttec_firmware(port, _quiet(), timeout_s=1.0)

    assert result == "fw 2.16"
    # Confirm we sent the right query and reset the input buffer first
    assert port.input_resets == 1
    assert port.writes == [build_enttec_get_params_frame()]


def test_query_enttec_firmware_returns_none_on_silent_dongle():
    """No reply → returns None within timeout, doesn't raise."""
    port = _ScriptedSerial(read_script=[])
    result = query_enttec_firmware(port, _quiet(), timeout_s=0.05)
    assert result is None


def test_query_enttec_firmware_swallows_port_exceptions():
    """A misbehaving port shouldn't crash startup."""
    class _AngryPort:
        def reset_input_buffer(self): raise OSError("bad")
        def write(self, _data): raise OSError("worse")
        def flush(self): pass
        def read(self, _n): return b""

    result = query_enttec_firmware(_AngryPort(), _quiet(), timeout_s=0.05)
    assert result is None
