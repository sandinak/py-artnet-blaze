"""HTTP test panel: routes, JSON contract, override propagation."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

import pytest

from artnet_blaze.artnet import ArtNetReceiver
from artnet_blaze.controller import TestController
from artnet_blaze.dmx import DmxFixture
from artnet_blaze.http_api import (
    HttpServerThread,
    _fixture_values,
    _fixture_views,
    _strip_view_hex,
)
from artnet_blaze.poe import StripMapping
from artnet_blaze.sysinfo import StaticInfo


def _quiet():
    log = logging.getLogger("http-test")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


@pytest.fixture
def http_server():
    log = _quiet()
    rx = ArtNetReceiver("127.0.0.1", {0, 4}, log)
    controller = TestController(rx, min_hold_s=99)
    static = StaticInfo.collect("9.9.9-test")
    static.dmx_dongle = "/dev/ttyFAKE"
    static.dmx_protocol = "enttec_pro"
    static.dmx_firmware = "fw 1.42"
    static.unit_name = "US1"
    strips = [
        StripMapping(poe_channel=0, universe=0, offset=0,   pixel_count=4,
                     row=1, side="SR"),
        StripMapping(poe_channel=1, universe=0, offset=12,  pixel_count=4,
                     row=1, side="SL"),
        StripMapping(poe_channel=2, universe=4, offset=0,   pixel_count=2,
                     row=2, side="SR"),
    ]
    fixtures = [
        DmxFixture(
            universe=4, offset=100, dmx_start=1, length=26,
            name="bar SR",
            render={"kind": "rgb_bar", "sections": 8,
                    "intensity_at": 24, "strobe_at": 25},
        ),
        DmxFixture(
            universe=4, offset=200, dmx_start=27, length=8,
            name="par 1",
            render={"kind": "raw"},
        ),
    ]

    server = HttpServerThread(
        bind="127.0.0.1", port=0,  # ephemeral
        static=static, controller=controller, receiver=rx, strips=strips,
        fixtures=fixtures, log=log,
    )
    server.start()
    try:
        yield server, rx, controller, static
    finally:
        server.stop()


def _get(url: str) -> tuple[int, dict | str]:
    with urllib.request.urlopen(url, timeout=2.0) as r:
        body = r.read()
        ct = r.headers.get_content_type()
        if ct == "application/json":
            return r.status, json.loads(body)
        return r.status, body.decode()


def _post(url: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=2.0) as r:
        return r.status, json.loads(r.read())


def test_index_serves_html(http_server):
    server, *_ = http_server
    status, body = _get(f"http://127.0.0.1:{server.port}/")
    assert status == 200
    assert "<!doctype html>" in body
    assert "test panel" in body


def test_status_json_contract(http_server):
    server, _, _, static = http_server
    status, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    assert status == 200
    assert isinstance(body, dict)
    assert body["static"]["code_version"] == "9.9.9-test"
    assert body["static"]["dmx_firmware"] == "fw 1.42"
    assert body["override"] == {"active": False}
    assert body["dmx_active"] == {"0": False, "4": False}
    assert "ip_addresses" in body["live"]
    assert "process_uptime_human" in body["live"]


def test_post_white_sets_override(http_server):
    server, _, controller, _ = http_server
    status, body = _post(f"http://127.0.0.1:{server.port}/test/white")
    assert status == 200
    assert body == {"ok": True, "value": 0xFF}
    assert controller.current().value == 0xFF


def test_post_half_sets_override(http_server):
    server, _, controller, _ = http_server
    _post(f"http://127.0.0.1:{server.port}/test/half")
    assert controller.current().value == 0x80


def test_post_clear_drops_override(http_server):
    server, _, controller, _ = http_server
    controller.set_value(0xFF)
    _post(f"http://127.0.0.1:{server.port}/test/clear")
    assert controller.current() is None


def test_post_identify_installs_identify_override(http_server):
    from artnet_blaze.overrides import IdentifyOverride
    server, _, controller, _ = http_server
    status, body = _post(f"http://127.0.0.1:{server.port}/test/identify")
    assert status == 200
    assert body["ok"] is True
    assert body["kind"] == "identify"
    assert body["unit"] == "US1"
    cur = controller.current()
    assert isinstance(cur, IdentifyOverride)
    assert cur.unit_name == "US1"


def test_status_carries_unit_name(http_server):
    server, *_ = http_server
    _, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    assert body["static"]["unit_name"] == "US1"


def test_status_during_identify_shows_kind(http_server):
    server, _, controller, _ = http_server
    _post(f"http://127.0.0.1:{server.port}/test/identify")
    _, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    assert body["override"]["active"] is True
    assert body["override"]["kind"] == "identify"
    assert body["override"]["unit"] == "US1"


def test_status_reflects_active_override_and_dmx(http_server, artdmx):
    server, rx, controller, _ = http_server
    rx.handle(artdmx(0, b"\xAB" * 6))
    rx.buffers[0].last_seen = time.monotonic()
    controller.set_value(0xFF)

    _, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    assert body["override"]["active"] is True
    assert body["override"]["value"] == 0xFF
    assert body["dmx_active"]["0"] is True
    assert body["dmx_active"]["4"] is False


def test_unknown_route_returns_404(http_server):
    server, *_ = http_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"http://127.0.0.1:{server.port}/no-such-thing")
    assert exc.value.code == 404


def test_unknown_post_returns_404(http_server):
    server, *_ = http_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"http://127.0.0.1:{server.port}/test/nope")
    assert exc.value.code == 404


def test_healthz(http_server):
    server, *_ = http_server
    status, body = _get(f"http://127.0.0.1:{server.port}/healthz")
    assert status == 200
    assert body == {"ok": True}


def test_server_stop_is_idempotent():
    log = _quiet()
    rx = ArtNetReceiver("127.0.0.1", {0}, log)
    server = HttpServerThread(
        bind="127.0.0.1", port=0,
        static=StaticInfo.collect("test"),
        controller=TestController(rx),
        receiver=rx, strips=[], fixtures=[], log=log,
    )
    server.start()
    server.stop()
    server.stop()  # second call must not raise


# ── readiness + LED routes ──────────────────────────────────────


def test_test_led_route_returns_503_when_led_disabled(http_server):
    server, *_ = http_server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"http://127.0.0.1:{server.port}/test/led/green")
    assert exc.value.code == 503


def _server_with_led(strips=None, fixtures=None):
    """Build an HttpServerThread with a mocked status LED + readiness fn
    so we can exercise the LED routes and the readiness payload without
    grabbing real GPIOs or the full app."""
    from artnet_blaze.readiness import ReadinessReport
    from artnet_blaze.status_led import (
        Readiness, StatusLedThread, _NoopLed,
    )

    log = _quiet()
    rx = ArtNetReceiver("127.0.0.1", {0}, log)
    controller = TestController(rx)
    static = StaticInfo.collect("test")
    led = _NoopLed(log)
    state_holder = {"state": Readiness.READY}
    sled = StatusLedThread(
        led=led, evaluator=lambda: state_holder["state"], log=log,
        poll_interval_s=999,  # never auto-advances inside the test
    )
    sled._applied = Readiness.READY  # pretend it's been applied
    server = HttpServerThread(
        bind="127.0.0.1", port=0,
        static=static, controller=controller,
        receiver=rx, strips=strips or [], fixtures=fixtures or [],
        log=log,
        readiness_fn=lambda: ReadinessReport(
            state=state_holder["state"],
            checks={"network": True, "poe_port_open": True,
                    "dmx_port_open": None, "artnet_active":
                    state_holder["state"] == Readiness.READY},
        ),
        status_led=sled,
    )
    server.start()
    return server, sled, led, state_holder


def test_test_led_route_forces_color():
    from artnet_blaze.status_led import TEST_COLORS
    server, sled, led, _ = _server_with_led()
    try:
        status, body = _post(f"http://127.0.0.1:{server.port}/test/led/red")
        assert status == 200
        assert body == {"ok": True, "color": "red"}
        assert led.color == TEST_COLORS["red"]
    finally:
        server.stop()


def test_test_led_route_rejects_unknown_color():
    server, *_ = _server_with_led()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _post(f"http://127.0.0.1:{server.port}/test/led/chartreuse")
        assert exc.value.code == 400
    finally:
        server.stop()


def test_status_includes_readiness_when_evaluator_present():
    from artnet_blaze.status_led import Readiness
    server, _, _, state_holder = _server_with_led()
    try:
        _, body = _get(f"http://127.0.0.1:{server.port}/api/status")
        assert "readiness" in body
        assert body["readiness"]["state"] == "ready"
        assert body["readiness"]["led_enabled"] is True
        assert body["readiness"]["checks"]["network"] is True

        # Flip the evaluator to fault and re-check.
        state_holder["state"] = Readiness.FAULT
        _, body2 = _get(f"http://127.0.0.1:{server.port}/api/status")
        assert body2["readiness"]["state"] == "fault"
    finally:
        server.stop()


def test_status_omits_readiness_when_no_evaluator(http_server):
    """Default fixture has no readiness_fn → key absent from JSON."""
    server, *_ = http_server
    _, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    assert "readiness" not in body


# ── strip-view tests ────────────────────────────────────────────


def test_strip_view_hex_uses_universe_bytes_when_no_override():
    rx_data = b"\x10\x20\x30\x40\x50\x60\x70\x80\x90"  # 3 pixels of RGB
    universes = {0: rx_data + bytes(512 - len(rx_data))}
    strip = StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=3)
    out = _strip_view_hex(strip, universes, override=None)
    assert out == "102030405060708090"


def test_strip_view_hex_override_paints_all_leds_uniform():
    from artnet_blaze.overrides import UniformByteOverride
    universes = {0: bytes(512)}
    strip = StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=2)
    out = _strip_view_hex(strip, universes, override=UniformByteOverride(0xFF))
    assert out == "ff" * 6
    out2 = _strip_view_hex(strip, universes, override=UniformByteOverride(0x80))
    assert out2 == "80" * 6


def test_strip_view_hex_zero_pads_when_universe_short():
    universes = {0: b"\x12\x34"}  # only 2 bytes, strip wants 6
    strip = StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=2)
    out = _strip_view_hex(strip, universes, override=None)
    assert out == "123400000000"  # first 2 bytes preserved, rest zero-filled


def test_strip_view_hex_returns_zeros_when_universe_missing():
    strip = StripMapping(poe_channel=0, universe=99, offset=0, pixel_count=4)
    out = _strip_view_hex(strip, {0: bytes(512)}, override=None)
    assert out == "0" * (4 * 6)


def test_status_includes_strip_views(http_server, artdmx):
    server, rx, _, _ = http_server
    # Universe 0 carries known bytes; universe 4 left empty.
    rx.handle(artdmx(universe=0, data=b"\xAA\xBB\xCC\x11\x22\x33"
                                       + b"\x00" * 10
                                       + b"\xDE\xAD\xBE\xEF\x55\x66"))

    status, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    assert status == 200
    strips = body["strips"]
    assert len(strips) == 3

    # Strip 0: U0@0, 4 LEDs → first 12 bytes of universe 0
    assert strips[0]["channel"] == 0
    assert strips[0]["pixel_count"] == 4
    assert strips[0]["pixels"][:12] == "aabbcc112233"

    # Strip 1: U0@12, 4 LEDs → bytes 12..23 of universe 0.
    # Sent payload was 6 known + 10 zero + 6 known, so bytes 12..23 are
    # 4 zero bytes (12..15) followed by DE AD BE EF 55 66 (16..21) and
    # then 2 zero bytes (22..23) from the receiver buffer's zero-init.
    assert strips[1]["channel"] == 1
    assert strips[1]["pixels"] == "00000000" + "deadbeef" + "5566" + "0000"

    # Strip 2: U4@0, 2 LEDs → universe 4 had no traffic, so zeros
    assert strips[2]["channel"] == 2
    assert strips[2]["pixels"] == "0" * 12


def test_status_strip_views_reflect_active_override(http_server):
    server, _, controller, _ = http_server
    controller.set_value(0xFF)
    _, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    for strip in body["strips"]:
        # Every byte should be ff
        assert set(strip["pixels"]) == {"f"}


def test_status_strip_views_keys_stable(http_server):
    server, *_ = http_server
    _, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    for s in body["strips"]:
        assert set(s.keys()) == {
            "channel", "universe", "offset", "pixel_count",
            "row", "side", "pixels",
        }


# ── fixture-view tests ──────────────────────────────────────────


def test_fixture_values_uses_universe_bytes():
    fx = DmxFixture(universe=0, offset=0, dmx_start=1, length=4)
    universes = {0: b"\x10\x20\x30\x40" + bytes(508)}
    assert _fixture_values(fx, universes, override=None) == [16, 32, 48, 64]


def test_fixture_values_override_paints_uniform():
    from artnet_blaze.overrides import UniformByteOverride
    fx = DmxFixture(universe=0, offset=0, dmx_start=1, length=5)
    assert _fixture_values(fx, {}, override=UniformByteOverride(0xFF)) == [255] * 5


def test_fixture_values_zero_pads_short_universe():
    fx = DmxFixture(universe=0, offset=2, dmx_start=1, length=4)
    universes = {0: b"\x11\x22\x33"}
    assert _fixture_values(fx, universes, override=None) == [51, 0, 0, 0]


def test_fixture_views_emits_render_metadata(silent_log, artdmx):
    rx = ArtNetReceiver("127.0.0.1", {4}, silent_log)
    rx.handle(artdmx(universe=4, data=bytes(range(30))))
    controller = TestController(rx, min_hold_s=99)
    fixtures = [
        DmxFixture(
            universe=4, offset=0, dmx_start=1, length=26,
            name="bar SR",
            render={"kind": "rgb_bar", "sections": 8,
                    "intensity_at": 24, "strobe_at": 25},
        ),
    ]
    out = _fixture_views(fixtures, rx, controller)
    assert len(out) == 1
    f = out[0]
    assert f["name"] == "bar SR"
    assert f["render"]["kind"] == "rgb_bar"
    assert f["render"]["sections"] == 8
    assert f["render"]["intensity_at"] == 24
    assert f["render"]["strobe_at"] == 25
    assert f["values"] == list(range(26))


def test_fixture_views_default_render_when_unset(silent_log):
    rx = ArtNetReceiver("127.0.0.1", {0}, silent_log)
    controller = TestController(rx)
    fixtures = [DmxFixture(universe=0, offset=0, dmx_start=1, length=3)]
    out = _fixture_views(fixtures, rx, controller)
    assert out[0]["render"] == {"kind": "raw"}


def test_status_includes_fixture_views(http_server, artdmx):
    server, rx, _, _ = http_server
    rx.handle(artdmx(
        universe=4,
        data=bytes(100) + bytes(range(26)) + bytes(74) + b"\x42" * 8,
    ))
    _, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    assert "fixtures" in body
    assert len(body["fixtures"]) == 2

    bar = body["fixtures"][0]
    assert bar["name"] == "bar SR"
    assert bar["values"] == list(range(26))

    par = body["fixtures"][1]
    assert par["name"] == "par 1"
    assert par["render"] == {"kind": "raw"}
    assert par["values"] == [0x42] * 8


def test_status_fixtures_reflect_override(http_server):
    server, _, controller, _ = http_server
    controller.set_value(0xFF)
    _, body = _get(f"http://127.0.0.1:{server.port}/api/status")
    for fx in body["fixtures"]:
        assert all(v == 255 for v in fx["values"])
