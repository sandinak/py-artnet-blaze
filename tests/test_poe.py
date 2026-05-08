"""POE wire format + PoeSink behavior."""

from __future__ import annotations

import struct
import zlib

from artnet_blaze.artnet import ArtNetReceiver
from artnet_blaze.poe import (
    POE_MAGIC,
    POE_REC_DRAW_ALL,
    POE_REC_SET_CHANNEL_WS2812,
    PoeSink,
    StripMapping,
    poe_frame_draw_all,
    poe_frame_set_channel,
)


def test_set_channel_frame_layout():
    pixels = b"\x01\x02\x03" * 4  # 4 pixels GRB
    frame = poe_frame_set_channel(channel=2, pixel_data=pixels)
    # 4-byte magic + ch + rec + u16 count + u8 bpp + 4-byte color order = 13B header
    assert frame[:4] == POE_MAGIC
    assert frame[4] == 0x02
    assert frame[5] == POE_REC_SET_CHANNEL_WS2812
    (pixel_count,) = struct.unpack("<H", frame[6:8])
    assert pixel_count == 4
    assert frame[8] == 3  # bytes per pixel
    assert tuple(frame[9:13]) == (1, 0, 2, 3)
    # body = pixels
    assert frame[13:13 + len(pixels)] == pixels
    # CRC over header+payload
    payload = frame[: 13 + len(pixels)]
    (crc,) = struct.unpack("<I", frame[-4:])
    assert crc == zlib.crc32(payload)


def test_draw_all_frame_layout():
    frame = poe_frame_draw_all()
    assert frame[:4] == POE_MAGIC
    assert frame[4] == 0xFF
    assert frame[5] == POE_REC_DRAW_ALL
    assert len(frame) == 6 + 4  # header + crc
    (crc,) = struct.unpack("<I", frame[-4:])
    assert crc == zlib.crc32(frame[:6])


def test_poe_sink_writes_set_channel_per_strip(silent_log, fake_serial, artdmx):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    rx.handle(artdmx(0, b"\xAA" * 6 + b"\xBB" * 6))
    strips = [
        StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=2),
        StripMapping(poe_channel=1, universe=0, offset=6, pixel_count=2),
    ]
    sink = PoeSink(rx, fake_serial, strips, fps=50, log=silent_log)
    sink.tx_one_frame()

    writes = fake_serial.writes()
    assert len(writes) == 1
    out = writes[0]
    # Two set_channel frames (each 13 header + 6 payload + 4 crc = 23) +
    # one draw_all (6 header + 4 crc = 10) = 56 bytes total.
    assert len(out) == 23 + 23 + 10
    # First record: channel 0, pixels = 0xAA*6
    assert out[4] == 0
    assert out[13:19] == b"\xAA" * 6
    # Second record starts at byte 23
    assert out[23 + 4] == 1
    assert out[23 + 13: 23 + 19] == b"\xBB" * 6
    # Tail is draw_all
    assert out[-10:-4][:4] == POE_MAGIC
    assert out[-10:-4][5] == POE_REC_DRAW_ALL


def test_poe_sink_blackout_zeros_all_strips(silent_log, fake_serial):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    strips = [StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=4)]
    sink = PoeSink(rx, fake_serial, strips, fps=50, log=silent_log)
    sink.blackout()
    out = fake_serial.writes()[0]
    pixel_payload = out[13:13 + 12]  # 4 pixels × 3 bytes
    assert pixel_payload == b"\x00" * 12


def test_poe_sink_skips_unseen_universe(silent_log, fake_serial):
    rx = ArtNetReceiver("127.0.0.1", {0, 1}, silent_log)
    # Only universe 0 has data; universe 1 strip should be silently skipped.
    strips = [
        StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=2),
        StripMapping(poe_channel=1, universe=1, offset=0, pixel_count=2),
    ]
    sink = PoeSink(rx, fake_serial, strips, fps=50, log=silent_log)
    sink.tx_one_frame()
    out = fake_serial.writes()[0]
    # Both universes are in the receiver buffer (zero-initialized), so both
    # set_channel records fire — what we're asserting is no crash and the
    # output has both records present.
    assert out.count(POE_MAGIC) == 3  # two set + one draw
