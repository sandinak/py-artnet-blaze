"""Pixelblaze Output Expander (POE) protocol + sink.

Wire format per https://github.com/simap/pixelblaze_output_expander.
A "draw" packet is a concatenation of N `set channel` records (one per
strip we want to update this frame) followed by a single `draw all`
record that latches every channel simultaneously.
"""

from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol

from .sink import Sink

if TYPE_CHECKING:
    from .artnet import ArtNetReceiver
    from .controller import TestController


POE_MAGIC = b"UPXL"
POE_REC_SET_CHANNEL_WS2812 = 0x01
POE_REC_DRAW_ALL = 0x02


class _SerialLike(Protocol):
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


def poe_frame_set_channel(
    channel: int,
    pixel_data: bytes,
    bytes_per_pixel: int = 3,
    color_order: tuple[int, int, int, int] = (1, 0, 2, 3),
) -> bytes:
    """Build a 'set channel' POE record carrying WS2812 pixel data.

    color_order: byte indices for (R, G, B, W) positions in output stream.
    Default (1, 0, 2, 3) = GRB, native WS2812 wire order.
    """
    pixel_count = len(pixel_data) // bytes_per_pixel
    header = struct.pack(
        "<4sBBHBBBBB",
        POE_MAGIC,
        channel & 0xFF,
        POE_REC_SET_CHANNEL_WS2812,
        pixel_count,
        bytes_per_pixel,
        color_order[0],
        color_order[1],
        color_order[2],
        color_order[3],
    )
    payload = header + pixel_data
    crc = struct.pack("<I", zlib.crc32(payload))
    return payload + crc


def poe_frame_draw_all() -> bytes:
    """Build a 'latch all channels simultaneously' POE record."""
    header = struct.pack("<4sBB", POE_MAGIC, 0xFF, POE_REC_DRAW_ALL)
    crc = struct.pack("<I", zlib.crc32(header))
    return header + crc


@dataclass
class StripMapping:
    """One physical WS2812 strip: source universe → POE channel.

    `row` and `side` are optional metadata used by the identify test
    pattern to lay out staircase + SL line + unit-name text across the
    physical step. They have no effect on the wire output.
        row:  1-based step row, top → bottom (typical: 1..4)
        side: "SR" or "SL" — which end of the row this strip drives
    """
    poe_channel: int       # 0..7
    universe: int          # ArtNet universe index (flat)
    offset: int            # byte offset into universe (0-based)
    pixel_count: int       # LEDs on the strip
    row: Optional[int] = None
    side: Optional[str] = None


class PoeSink(Sink):
    """POE output: concatenates set-channel records + draw_all per tick."""

    def __init__(
        self,
        receiver: "ArtNetReceiver",
        port: _SerialLike,
        strips: list[StripMapping],
        fps: float,
        log: logging.Logger,
        controller: "TestController | None" = None,
    ) -> None:
        super().__init__("poe-tx", receiver, fps, log)
        self.port = port
        self.strips = strips
        self.controller = controller

    def tx_one_frame(self) -> None:
        override = self.controller.current() if self.controller else None
        out = bytearray()
        if override is not None:
            for strip in self.strips:
                pixel_data = override.strip_pixels(strip)
                out += poe_frame_set_channel(strip.poe_channel, pixel_data)
        else:
            universes = self.receiver.snapshot()
            for strip in self.strips:
                uni_data = universes.get(strip.universe)
                if uni_data is None:
                    continue
                start = strip.offset
                end = min(start + strip.pixel_count * 3, len(uni_data))
                pixel_data = bytes(uni_data[start:end])
                out += poe_frame_set_channel(strip.poe_channel, pixel_data)
        out += poe_frame_draw_all()
        self.port.write(bytes(out))

    def blackout(self) -> None:
        out = bytearray()
        for s in self.strips:
            out += poe_frame_set_channel(
                s.poe_channel, bytes(s.pixel_count * 3)
            )
        out += poe_frame_draw_all()
        self.port.write(bytes(out))
        self.port.flush()
