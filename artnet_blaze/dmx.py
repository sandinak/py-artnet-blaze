"""USB DMX output: Enttec USB DMX Pro and Open DMX USB.

Two protocols, one sink:

  * **Enttec USB DMX Pro** speaks framed serial at 115200 baud:
        0x7E <label> <len_lo> <len_hi> <data...> 0xE7
    Label 0x06 = "Send DMX packet". Data = start_code (0x00) + 512 slots.
    The dongle generates BREAK/MAB internally, so we just write frames
    as fast as needed; pacing is FPS-driven on our side.

  * **Open DMX USB** (raw FTDI) needs us to generate the DMX line state:
    set the port to 250000 baud / 8N2, assert BREAK for ≥88 µs, release
    for ≥8 µs MAB, then write start_code (0x00) + 512 slots. Timing on
    non-RT Linux is best-effort; occasional flicker is the protocol, not
    the code.

A DmxSink builds the outgoing 512-slot frame each tick by copying bytes
from configured ArtNet universes/offsets into target DMX slot ranges.
"""

from __future__ import annotations

import logging
import struct
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol

from .sink import Sink

if TYPE_CHECKING:
    from .artnet import ArtNetReceiver
    from .controller import TestController


PROTOCOL_ENTTEC_PRO = "enttec_pro"
PROTOCOL_OPEN_DMX = "open_dmx"

ENTTEC_START = 0x7E
ENTTEC_END = 0xE7
ENTTEC_LABEL_SEND_DMX = 0x06
ENTTEC_LABEL_GET_PARAMS = 0x03

DMX_START_CODE = 0x00
DMX_SLOT_COUNT = 512

# DMX timing minimums (µs). Erring on the long side is fine.
OPEN_DMX_BREAK_US = 110
OPEN_DMX_MAB_US = 12


class _SerialLike(Protocol):
    """Subset of pyserial.Serial used by DmxSink."""
    break_condition: bool
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


@dataclass
class DmxFixture:
    """Maps a slice of an ArtNet universe into the outgoing DMX frame.

    `dmx_start` is 1-based to match how DMX channels are addressed on
    fixtures. A fixture pulls `length` bytes from `universe[offset:]`
    and places them at slots `dmx_start..dmx_start+length-1` on the wire.

    `name` and `render` are presentation-only — used by the HTTP test
    panel to label the fixture and decide how to visualize its bytes.
    They have no effect on the wire output.

    `render` shape (validated in config.build_fixtures):
        {"kind": "raw"}                              # default; numeric chips
        {"kind": "rgb_bar", "sections": 8,
         "intensity_at": 24, "strobe_at": 25}        # 8 RGB sections + DIM/STR
    """
    universe: int
    offset: int        # byte offset into source universe (0..511)
    dmx_start: int     # 1-based DMX slot on the dongle (1..512)
    length: int        # number of channels copied
    name: str = ""
    render: Optional[dict] = None


def build_enttec_get_params_frame(user_size: int = 0) -> bytes:
    """'Get Widget Parameters' query: 0x7E 0x03 <len> <data> 0xE7."""
    payload = struct.pack("<H", user_size)  # 2-byte user-config size request
    return (
        bytes([ENTTEC_START, ENTTEC_LABEL_GET_PARAMS])
        + struct.pack("<H", len(payload))
        + payload
        + bytes([ENTTEC_END])
    )


def parse_enttec_params_response(buf: bytes) -> Optional[str]:
    """Parse a 'Get Widget Parameters' reply, return 'fwM.NN' or None.

    Frame layout: 0x7E 0x03 <len_lo> <len_hi> <fw_lsb> <fw_msb> <break>
                  <mab> <refresh_rate> [user_data...] 0xE7
    """
    start = buf.find(bytes([ENTTEC_START, ENTTEC_LABEL_GET_PARAMS]))
    if start < 0:
        return None
    end = buf.find(ENTTEC_END, start)
    if end < 0 or end - start < 9:
        return None
    fw_lsb = buf[start + 4]
    fw_msb = buf[start + 5]
    return f"fw {fw_msb}.{fw_lsb:02d}"


def query_enttec_firmware(port, log, timeout_s: float = 0.5) -> Optional[str]:
    """Send 'Get Widget Parameters' and read the reply.

    The port should already be open. We tolerate junk on the line (some
    dongles emit a banner) by scanning for the start byte.
    """
    try:
        if hasattr(port, "reset_input_buffer"):
            port.reset_input_buffer()
        port.write(build_enttec_get_params_frame())
        if hasattr(port, "flush"):
            port.flush()
        deadline = time.monotonic() + timeout_s
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = port.read(64) if hasattr(port, "read") else b""
            if chunk:
                buf += chunk
                if ENTTEC_END in buf:
                    break
            else:
                time.sleep(0.01)
        return parse_enttec_params_response(bytes(buf))
    except Exception as e:
        log.warning("Enttec firmware query failed: %s", e)
        return None


def build_enttec_pro_frame(slots: bytes) -> bytes:
    """Wrap 512 DMX slots in an Enttec USB DMX Pro 'send DMX' message."""
    if len(slots) != DMX_SLOT_COUNT:
        raise ValueError(f"slots must be {DMX_SLOT_COUNT} bytes, got {len(slots)}")
    payload = bytes([DMX_START_CODE]) + slots
    return (
        bytes([ENTTEC_START, ENTTEC_LABEL_SEND_DMX])
        + struct.pack("<H", len(payload))
        + payload
        + bytes([ENTTEC_END])
    )


def build_dmx_frame_payload(slots: bytes) -> bytes:
    """Open-DMX wire payload: start code + 512 slots (no break/MAB)."""
    if len(slots) != DMX_SLOT_COUNT:
        raise ValueError(f"slots must be {DMX_SLOT_COUNT} bytes, got {len(slots)}")
    return bytes([DMX_START_CODE]) + slots


def merge_fixtures(
    universes: dict[int, bytes],
    fixtures: list[DmxFixture],
) -> bytearray:
    """Build the outgoing 512-slot DMX buffer from fixture mappings.

    Slots not covered by any fixture stay at zero. Source data missing
    (universe never seen) leaves those slots at zero too — the same
    behavior as POE, which simply skips strips on absent universes.
    """
    out = bytearray(DMX_SLOT_COUNT)
    for fx in fixtures:
        uni = universes.get(fx.universe)
        if uni is None:
            continue
        src_end = min(fx.offset + fx.length, len(uni))
        chunk = uni[fx.offset:src_end]
        dst = fx.dmx_start - 1
        out[dst:dst + len(chunk)] = chunk
    return out


class DmxSink(Sink):
    """USB DMX output sink. Driver chosen by `protocol`."""

    def __init__(
        self,
        receiver: "ArtNetReceiver",
        port: _SerialLike,
        fixtures: list[DmxFixture],
        protocol: str,
        fps: float,
        log: logging.Logger,
        sleep_fn=time.sleep,
        controller: "TestController | None" = None,
    ) -> None:
        super().__init__("dmx-tx", receiver, fps, log)
        if protocol not in (PROTOCOL_ENTTEC_PRO, PROTOCOL_OPEN_DMX):
            raise ValueError(f"unknown DMX protocol: {protocol!r}")
        self.port = port
        self.fixtures = fixtures
        self.protocol = protocol
        self._sleep = sleep_fn
        self.controller = controller

    def tx_one_frame(self) -> None:
        override = self.controller.current() if self.controller else None
        if override is not None:
            slots = self._slots_from_override(override)
        else:
            universes = self.receiver.snapshot()
            slots = bytes(merge_fixtures(universes, self.fixtures))
        if self.protocol == PROTOCOL_ENTTEC_PRO:
            self.port.write(build_enttec_pro_frame(slots))
        else:
            self._tx_open_dmx(slots)

    def _slots_from_override(self, override) -> bytes:
        """Render the override into a 512-slot DMX frame.

        The override decides what each fixture's channels should be;
        slots not covered by any fixture stay at zero.
        """
        out = bytearray(DMX_SLOT_COUNT)
        for fx in self.fixtures:
            chunk = override.dmx_values(fx)
            if len(chunk) < fx.length:
                chunk = chunk + bytes(fx.length - len(chunk))
            elif len(chunk) > fx.length:
                chunk = chunk[:fx.length]
            dst = fx.dmx_start - 1
            out[dst:dst + fx.length] = chunk
        return bytes(out)

    def _tx_open_dmx(self, slots: bytes) -> None:
        # BREAK + MAB are the only "manual" parts; the byte stream itself
        # is just a normal write at 250000 baud / 8N2 (configured at open).
        self.port.break_condition = True
        self._sleep(OPEN_DMX_BREAK_US / 1_000_000)
        self.port.break_condition = False
        self._sleep(OPEN_DMX_MAB_US / 1_000_000)
        self.port.write(build_dmx_frame_payload(slots))

    def blackout(self) -> None:
        zeros = bytes(DMX_SLOT_COUNT)
        if self.protocol == PROTOCOL_ENTTEC_PRO:
            self.port.write(build_enttec_pro_frame(zeros))
        else:
            self._tx_open_dmx(zeros)
        self.port.flush()
