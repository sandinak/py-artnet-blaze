"""Shared test fixtures: fake serial port, packet builders, dummy logger."""

from __future__ import annotations

import logging
import struct

import pytest


class FakeSerial:
    """In-memory pyserial.Serial stand-in.

    Captures `write()` payloads and `break_condition` transitions in order
    so DMX BREAK/MAB sequencing can be asserted without hardware.
    """

    def __init__(self) -> None:
        self.events: list[tuple] = []  # ("write", bytes) | ("break", bool) | ("flush",) | ("close",)
        self._break = False

    @property
    def break_condition(self) -> bool:
        return self._break

    @break_condition.setter
    def break_condition(self, value: bool) -> None:
        self._break = bool(value)
        self.events.append(("break", self._break))

    def write(self, data: bytes) -> int:
        self.events.append(("write", bytes(data)))
        return len(data)

    def flush(self) -> None:
        self.events.append(("flush",))

    def close(self) -> None:
        self.events.append(("close",))

    def read(self, _n: int = 1) -> bytes:
        # No reply path for plain FakeSerial. Tests that need a scripted
        # reply build their own subclass.
        return b""

    def reset_input_buffer(self) -> None:
        self.events.append(("reset_input_buffer",))

    def writes(self) -> list[bytes]:
        return [e[1] for e in self.events if e[0] == "write"]


@pytest.fixture
def fake_serial() -> FakeSerial:
    return FakeSerial()


@pytest.fixture
def silent_log() -> logging.Logger:
    log = logging.getLogger("test")
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


def build_artdmx(universe: int, data: bytes, sequence: int = 0) -> bytes:
    """Construct a valid ArtDmx packet for tests."""
    return (
        b"Art-Net\x00"
        + struct.pack("<H", 0x5000)        # opcode
        + struct.pack(">H", 14)            # protver
        + bytes([sequence, 0])             # sequence, physical
        + struct.pack("<H", universe)
        + struct.pack(">H", len(data))
        + data
    )


@pytest.fixture
def artdmx():
    return build_artdmx
