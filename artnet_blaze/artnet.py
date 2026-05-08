"""ArtNet ArtDmx reception.

Listens on UDP/6454, parses ArtDmx packets, and stores the most recent
512-byte payload for each subscribed universe in a thread-safe buffer.
Sinks read from the receiver via `snapshot()`; input jitter is decoupled
from output cadence by the receiver→snapshot→sink-tick path.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


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


@dataclass
class UniverseBuffer:
    """Rolling per-universe DMX state."""
    data: bytearray = field(default_factory=lambda: bytearray(512))
    last_seen: float = 0.0
    sequence: int = 0
    rx_count: int = 0


def parse_artdmx(pkt: bytes) -> Optional[tuple[int, int, bytes]]:
    """Parse one ArtDmx packet.

    Returns (universe, sequence, data) on success, or None if the packet
    is not ArtDmx (wrong magic, wrong opcode, too short).
    """
    if len(pkt) < 18 or pkt[:8] != ARTNET_ID:
        return None
    (opcode,) = struct.unpack("<H", pkt[8:10])
    if opcode != OP_OUTPUT:
        return None
    seq = pkt[12]
    (universe,) = struct.unpack("<H", pkt[14:16])
    (length,) = struct.unpack(">H", pkt[16:18])
    data = pkt[18:18 + length]
    return universe, seq, data


class ArtNetReceiver(threading.Thread):
    """Listens for ArtDmx packets on the configured bind address."""

    def __init__(
        self,
        bind_addr: str,
        universes: set[int],
        log: logging.Logger,
        port: int = ARTNET_PORT,
    ) -> None:
        super().__init__(daemon=True, name="artnet-rx")
        self.bind_addr = bind_addr
        self.bind_port = port
        self.wanted = set(universes)
        self.log = log
        self.buffers: dict[int, UniverseBuffer] = {
            u: UniverseBuffer() for u in self.wanted
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
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sock.bind((self.bind_addr, self.bind_port))
        self.log.info(
            "ArtNet listening on %s:%d, universes=%s",
            self.bind_addr, self.bind_port, sorted(self.wanted),
        )
        while not self._stop.is_set():
            try:
                pkt, _src = self._sock.recvfrom(1500)
            except OSError:
                break
            self.handle(pkt)

    def handle(self, pkt: bytes) -> None:
        """Public so tests can feed crafted packets directly."""
        parsed = parse_artdmx(pkt)
        if parsed is None:
            return
        universe, seq, data = parsed
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
