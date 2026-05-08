"""ArtNet packet parsing + receiver buffering + real UDP socket loop."""

from __future__ import annotations

import socket
import time

from artnet_blaze.artnet import ArtNetReceiver, parse_artdmx


def test_parse_valid_artdmx(artdmx):
    pkt = artdmx(universe=3, data=b"\x11\x22\x33\x44", sequence=42)
    parsed = parse_artdmx(pkt)
    assert parsed is not None
    universe, seq, data = parsed
    assert universe == 3
    assert seq == 42
    assert data == b"\x11\x22\x33\x44"


def test_parse_rejects_wrong_magic():
    assert parse_artdmx(b"NOTART\x00\x00" + b"\x00" * 20) is None


def test_parse_rejects_short_packet():
    assert parse_artdmx(b"Art-Net\x00\x00\x50") is None


def test_parse_rejects_non_dmx_opcode(artdmx):
    pkt = bytearray(artdmx(0, b"\x00"))
    pkt[8:10] = (0x21).to_bytes(2, "little")  # ArtPoll opcode
    assert parse_artdmx(bytes(pkt)) is None


def test_receiver_handle_stores_into_buffer(silent_log, artdmx):
    rx = ArtNetReceiver("127.0.0.1", {0, 1}, silent_log)
    rx.handle(artdmx(universe=1, data=b"\xAA" * 10))
    assert rx.frames_rx == 1
    assert rx.buffers[1].data[:10] == b"\xAA" * 10
    # other universe untouched
    assert all(b == 0 for b in rx.buffers[0].data)


def test_receiver_counts_strays(silent_log, artdmx):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    rx.handle(artdmx(universe=99, data=b"\x01\x02"))
    assert rx.frames_rx == 0
    assert rx.stray_rx == 1


def test_receiver_short_data_does_not_overrun(silent_log, artdmx):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    rx.handle(artdmx(universe=0, data=b"\xFF" * 600))  # claim larger than 512
    # Buffer is fixed at 512; we just want no crash and no overflow.
    assert len(rx.buffers[0].data) == 512


def test_receiver_snapshot_is_stable_copy(silent_log, artdmx):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    rx.handle(artdmx(0, b"\x01\x02\x03"))
    snap = rx.snapshot()
    rx.handle(artdmx(0, b"\xFF\xFF\xFF"))  # mutate after snapshot
    assert snap[0][:3] == b"\x01\x02\x03"


def _ephemeral_port() -> int:
    """Return a free UDP port on 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_receiver_run_loop_processes_real_udp_packet(silent_log, artdmx):
    """Bind a real UDP socket on an ephemeral port, send a real packet
    via sendto, and confirm the receiver thread parsed and stored it."""
    port = _ephemeral_port()
    rx = ArtNetReceiver("127.0.0.1", {2}, silent_log, port=port)
    rx.start()
    try:
        # Wait briefly for the bind in the worker thread to complete.
        # If we send before bind, the packet is dropped silently.
        deadline = time.monotonic() + 1.0
        while rx._sock is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rx._sock is not None, "receiver never opened its socket"

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            payload = b"\xDE\xAD\xBE\xEF\x55"
            client.sendto(artdmx(universe=2, data=payload), ("127.0.0.1", port))
        finally:
            client.close()

        # Poll for delivery — the OS schedules the recv in the worker thread.
        deadline = time.monotonic() + 1.0
        while rx.frames_rx == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rx.frames_rx == 1, "packet was not received in time"
        assert rx.buffers[2].data[:5] == payload
    finally:
        rx.stop()
        rx.join(timeout=1.0)
        assert not rx.is_alive()


def test_receiver_run_loop_ignores_non_artnet_traffic(silent_log):
    """Random UDP traffic on the listen port should not be counted."""
    port = _ephemeral_port()
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log, port=port)
    rx.start()
    try:
        deadline = time.monotonic() + 1.0
        while rx._sock is None and time.monotonic() < deadline:
            time.sleep(0.01)

        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            client.sendto(b"hello there", ("127.0.0.1", port))
        finally:
            client.close()

        # Give the worker time to drain the socket
        time.sleep(0.05)
        assert rx.frames_rx == 0
        assert rx.stray_rx == 0
    finally:
        rx.stop()
        rx.join(timeout=1.0)
