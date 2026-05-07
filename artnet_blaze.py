#!/usr/bin/env python3
"""
py-artnet-blaze: ArtNet → Pixelblaze Output Expander bridge daemon

Receives ArtNet ArtDmx packets on UDP 6454 and forwards pixel data
to a Pixelblaze Output Expander (POE) over serial, driving up to 8
parallel WS281x channels per expander.

Designed for Evolution Show Choir "Step" units:
  - 8 WS2812 strips per step, laid out top-to-bottom, SR-to-SL
  - 2 strips per ArtNet universe (4 universes default)
  - QLC+ upstream as the show controller

Notes:
  - Decouples input (ArtNet jitter) from output (fixed FPS tick).
  - Partial frames OK: if a universe hasn't arrived yet, its last
    known state is re-sent rather than stalling the tick.
  - On SIGTERM/SIGINT: blacks out strips before exiting.
  - POE wire format per the protocol documented at
    https://github.com/simap/pixelblaze_output_expander. Cross-check
    against current firmware if byte layout changes in future revs.
"""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import struct
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import serial  # pyserial
import yaml


# ───────────────────── ArtNet constants ─────────────────────

ARTNET_PORT = 6454
ARTNET_ID = b"Art-Net\x00"
OP_OUTPUT = 0x5000  # ArtDmx

# ArtDmx packet layout (bytes):
#   0..7   ID "Art-Net\0"
#   8..9   OpCode (LE)
#   10..11 ProtVer (BE) = 14
#   12     Sequence
#   13     Physical
#   14..15 Universe (LE) — low 4 = universe, next 4 = subnet, next 7 = net
#   16..17 Length (BE) — channels in data
#   18..   DMX data


# ───────────────────── POE protocol ─────────────────────

POE_MAGIC = b"UPXL"
POE_REC_SET_CHANNEL_WS2812 = 0x01
POE_REC_DRAW_ALL = 0x02


def poe_frame_set_channel(
    channel: int,
    pixel_data: bytes,
    bytes_per_pixel: int = 3,
    color_order: tuple = (1, 0, 2, 3),
) -> bytes:
    """
    Build a 'set channel' POE record carrying WS2812 pixel data.

    color_order: byte indices for (R, G, B, W) positions in output stream.
    Default (1, 0, 2, 3) = GRB, which is WS2812 native wire order.
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


# ───────────────────── ArtNet receiver ─────────────────────

@dataclass
class UniverseBuffer:
    """Rolling per-universe DMX state."""
    data: bytearray = field(default_factory=lambda: bytearray(512))
    last_seen: float = 0.0
    sequence: int = 0
    rx_count: int = 0


class ArtNetReceiver(threading.Thread):
    """Listens for ArtDmx packets on the configured bind address."""

    def __init__(
        self,
        bind_addr: str,
        universes: set[int],
        log: logging.Logger,
    ) -> None:
        super().__init__(daemon=True, name="artnet-rx")
        self.bind_addr = bind_addr
        self.wanted = universes
        self.log = log
        self.buffers: dict[int, UniverseBuffer] = {
            u: UniverseBuffer() for u in universes
        }
        self.lock = threading.Lock()
        self.frames_rx = 0
        self.stray_rx = 0
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def run(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Also accept broadcast (QLC+ sometimes broadcasts)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind((self.bind_addr, ARTNET_PORT))
        self.log.info(
            "ArtNet listening on %s:%d, universes=%s",
            self.bind_addr, ARTNET_PORT, sorted(self.wanted),
        )
        while not self._stop.is_set():
            try:
                pkt, _src = self._sock.recvfrom(1500)
            except OSError:
                break
            self._handle(pkt)

    def _handle(self, pkt: bytes) -> None:
        if len(pkt) < 18 or pkt[:8] != ARTNET_ID:
            return
        (opcode,) = struct.unpack("<H", pkt[8:10])
        if opcode != OP_OUTPUT:
            return
        seq = pkt[12]
        (universe,) = struct.unpack("<H", pkt[14:16])
        (length,) = struct.unpack(">H", pkt[16:18])
        data = pkt[18:18 + length]
        if universe not in self.wanted:
            self.stray_rx += 1
            return
        with self.lock:
            buf = self.buffers[universe]
            n = min(len(data), len(buf.data))
            buf.data[:n] = data[:n]
            buf.last_seen = time.monotonic()
            buf.sequence = seq
            buf.rx_count += 1
            self.frames_rx += 1

    def snapshot(self) -> dict[int, bytes]:
        """Return a stable copy of all universe data."""
        with self.lock:
            return {u: bytes(b.data) for u, b in self.buffers.items()}


# ───────────────────── Bridge core ─────────────────────

@dataclass
class StripMapping:
    """Describes one physical WS2812 strip: source universe → POE channel."""
    poe_channel: int       # 0..7
    universe: int          # ArtNet universe index (flat, not net/subnet split)
    offset: int            # byte offset into universe (0-based, must be ≤ 509)
    pixel_count: int       # LEDs on the strip


class Bridge:
    """
    Fixed-tick forwarder: ArtNet state → POE wire packets.
    Input jitter doesn't affect output cadence.
    """

    def __init__(
        self,
        receiver: ArtNetReceiver,
        port: serial.Serial,
        strips: list[StripMapping],
        fps: float,
        log: logging.Logger,
    ) -> None:
        self.receiver = receiver
        self.port = port
        self.strips = strips
        self.period = 1.0 / fps
        self.log = log
        self._stop = threading.Event()
        self.frames_tx = 0
        self.tx_errors = 0
        self.late_ticks = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        next_tick = time.monotonic()
        while not self._stop.is_set():
            next_tick += self.period
            self._tx_one_frame()
            sleep = next_tick - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            elif sleep < -self.period:
                # More than one period behind — resync rather than spiral
                self.late_ticks += 1
                if self.late_ticks % 10 == 1:
                    self.log.warning(
                        "tx loop behind by %.1fms, resyncing", -sleep * 1000
                    )
                next_tick = time.monotonic()

    def _tx_one_frame(self) -> None:
        universes = self.receiver.snapshot()
        try:
            out = bytearray()
            for strip in self.strips:
                uni_data = universes.get(strip.universe)
                if uni_data is None:
                    continue
                start = strip.offset
                end = start + strip.pixel_count * 3
                if end > len(uni_data):
                    # Silent truncate; config error will show in logs
                    end = len(uni_data)
                pixel_data = bytes(uni_data[start:end])
                out += poe_frame_set_channel(strip.poe_channel, pixel_data)
            out += poe_frame_draw_all()
            self.port.write(out)
            self.frames_tx += 1
        except (serial.SerialException, OSError) as e:
            self.tx_errors += 1
            self.log.error("serial write failed: %s", e)


# ───────────────────── Config ─────────────────────

DEFAULT_CONFIG: dict = {
    "artnet": {"bind": "0.0.0.0"},
    "serial": {"device": "/dev/serial0", "baudrate": 2_000_000},
    "bridge": {"fps": 50},
    # Evolution step default: 4 universes, 2 strips per universe,
    # 60 LED/m × 2.4 m ≈ 144 LEDs per strip = 432 bytes per strip.
    # POE channels 0..7 laid out top-to-bottom, SR-then-SL within each U.
    "strips": [
        {"poe_channel": 0, "universe": 0, "offset": 0,   "pixel_count": 144},
        {"poe_channel": 1, "universe": 0, "offset": 432, "pixel_count": 144},
        {"poe_channel": 2, "universe": 1, "offset": 0,   "pixel_count": 144},
        {"poe_channel": 3, "universe": 1, "offset": 432, "pixel_count": 144},
        {"poe_channel": 4, "universe": 2, "offset": 0,   "pixel_count": 144},
        {"poe_channel": 5, "universe": 2, "offset": 432, "pixel_count": 144},
        {"poe_channel": 6, "universe": 3, "offset": 0,   "pixel_count": 144},
        {"poe_channel": 7, "universe": 3, "offset": 432, "pixel_count": 144},
    ],
    "logging": {"level": "INFO", "stats_interval_s": 10},
}


def load_config(path: Optional[Path]) -> dict:
    if path is None:
        return DEFAULT_CONFIG
    with open(path) as f:
        user = yaml.safe_load(f) or {}
    merged = dict(DEFAULT_CONFIG)
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def build_strips(raw: list[dict]) -> list[StripMapping]:
    out = []
    for s in raw:
        m = StripMapping(
            poe_channel=int(s["poe_channel"]),
            universe=int(s["universe"]),
            offset=int(s["offset"]),
            pixel_count=int(s["pixel_count"]),
        )
        if not 0 <= m.poe_channel <= 7:
            raise ValueError(f"poe_channel out of range: {m.poe_channel}")
        if m.offset + m.pixel_count * 3 > 512:
            raise ValueError(
                f"strip on ch{m.poe_channel} overruns universe "
                f"{m.universe}: offset={m.offset} pixels={m.pixel_count}"
            )
        out.append(m)
    return out


# ───────────────────── Main ─────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="ArtNet → Pixelblaze Output Expander bridge",
    )
    ap.add_argument("-c", "--config", type=Path, help="YAML config path")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    level = logging.DEBUG if args.verbose else getattr(
        logging, str(cfg["logging"]["level"]).upper(), logging.INFO
    )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("blaze")

    strips = build_strips(cfg["strips"])
    universes = {s.universe for s in strips}

    port = serial.Serial(
        cfg["serial"]["device"],
        baudrate=int(cfg["serial"]["baudrate"]),
        timeout=0,
        write_timeout=0.1,
    )
    log.info(
        "serial open: %s @ %d baud",
        cfg["serial"]["device"], cfg["serial"]["baudrate"],
    )

    rx = ArtNetReceiver(cfg["artnet"]["bind"], universes, log)
    br = Bridge(rx, port, strips, float(cfg["bridge"]["fps"]), log)
    rx.start()

    def handle_signal(signum, _frame):
        log.info("signal %d received, shutting down", signum)
        br.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    def stats_loop():
        interval = float(cfg["logging"]["stats_interval_s"])
        while not br._stop.is_set():
            time.sleep(interval)
            log.info(
                "rx=%d tx=%d tx_errors=%d late=%d stray=%d",
                rx.frames_rx, br.frames_tx, br.tx_errors,
                br.late_ticks, rx.stray_rx,
            )

    threading.Thread(target=stats_loop, daemon=True).start()

    try:
        br.run()
    finally:
        rx.stop()
        # Blackout all strips on clean shutdown
        try:
            blackout = bytearray()
            for s in strips:
                blackout += poe_frame_set_channel(
                    s.poe_channel, bytes(s.pixel_count * 3)
                )
            blackout += poe_frame_draw_all()
            port.write(blackout)
            port.flush()
        except Exception as e:
            log.warning("blackout on shutdown failed: %s", e)
        port.close()
        log.info("exited")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
