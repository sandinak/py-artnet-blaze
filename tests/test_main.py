"""main.py wiring: setup_app, shutdown_app, stats_line, CLI entry."""

from __future__ import annotations

import logging
import time

import pytest
import yaml

from artnet_blaze.main import main, setup_app, shutdown_app, stats_line
from tests.conftest import FakeSerial


def _quiet():
    log = logging.getLogger("main-test")
    log.handlers.clear()
    log.addHandler(logging.NullHandler())
    log.setLevel(logging.CRITICAL)
    return log


def _cfg_dmx_off():
    """Config with DMX disabled — POE only."""
    return {
        "artnet": {"bind": "127.0.0.1", "port": 0},  # ephemeral
        "serial": {"device": "fake-poe", "baudrate": 2_000_000},
        "bridge": {"fps": 200},
        "strips": [
            {"poe_channel": 0, "universe": 0, "offset": 0, "pixel_count": 8},
        ],
        "dmx": {"enabled": False, "device": "fake-dmx",
                "protocol": "enttec_pro", "fps": 40, "fixtures": []},
        # HTTP off by default in tests so the suite can run in parallel
        # without port-8080 conflicts. test_main_http_lifecycle re-enables.
        "http": {"enabled": False, "bind": "127.0.0.1", "port": 0},
        "logging": {"level": "INFO", "stats_interval_s": 10},
    }


def _cfg_dmx_on():
    cfg = _cfg_dmx_off()
    cfg["dmx"] = {
        "enabled": True, "device": "fake-dmx", "protocol": "enttec_pro",
        "fps": 200,
        "fixtures": [{"universe": 4, "offset": 0, "dmx_start": 1, "length": 8}],
    }
    return cfg


def test_setup_app_poe_only():
    poe_port = FakeSerial()
    app = setup_app(
        _cfg_dmx_off(), _quiet(),
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: pytest.fail("DMX should not have been opened"),
    )
    assert app.poe_port is poe_port
    assert app.dmx_sink is None
    assert app.dmx_port is None
    assert app.sinks == [app.poe_sink]
    # Receiver subscribed to the single strip's universe
    assert app.rx.wanted == {0}


def test_setup_app_with_dmx_enabled():
    poe_port, dmx_port = FakeSerial(), FakeSerial()
    app = setup_app(
        _cfg_dmx_on(), _quiet(),
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: dmx_port,
    )
    assert app.dmx_sink is not None
    assert app.dmx_port is dmx_port
    # Receiver subscribed to union of POE (0) and DMX (4) universes
    assert app.rx.wanted == {0, 4}
    assert len(app.sinks) == 2


def test_setup_app_dmx_enabled_but_no_fixtures_warns_and_skips():
    cfg = _cfg_dmx_on()
    cfg["dmx"]["fixtures"] = []
    poe_port = FakeSerial()

    log = logging.getLogger("setup-no-fix")
    log.handlers.clear()
    seen: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, rec):
            seen.append(rec.getMessage())

    log.addHandler(_Capture())
    log.setLevel(logging.WARNING)

    app = setup_app(
        cfg, log,
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: pytest.fail("DMX should not have been opened"),
    )
    assert app.dmx_sink is None
    assert any("no fixtures" in m for m in seen)


def test_stats_line_contents():
    poe_port, dmx_port = FakeSerial(), FakeSerial()
    app = setup_app(
        _cfg_dmx_on(), _quiet(),
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: dmx_port,
    )
    line = stats_line(app)
    for token in ("rx=", "stray=", "poe_tx=", "poe_err=", "poe_late=",
                  "dmx_tx=", "dmx_err=", "dmx_late="):
        assert token in line


def test_stats_line_omits_dmx_when_disabled():
    poe_port = FakeSerial()
    app = setup_app(
        _cfg_dmx_off(), _quiet(),
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: pytest.fail("unused"),
    )
    line = stats_line(app)
    assert "poe_tx=" in line
    assert "dmx_tx=" not in line


def test_shutdown_app_stops_threads_and_closes_ports():
    poe_port, dmx_port = FakeSerial(), FakeSerial()
    log = _quiet()
    app = setup_app(
        _cfg_dmx_on(), log,
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: dmx_port,
    )
    app.rx.start()
    for s in app.sinks:
        s.start()
    time.sleep(0.03)  # let a few ticks happen

    shutdown_app(app, log)

    # Both sinks stopped, both ports closed
    assert not app.poe_sink.is_alive()
    assert not app.dmx_sink.is_alive()
    assert ("close",) in poe_port.events
    assert ("close",) in dmx_port.events
    # Some transmissions accumulated and a blackout was the last write on each
    assert app.poe_sink.frames_tx >= 1
    assert app.dmx_sink.frames_tx >= 1
    assert len(poe_port.writes()) >= 2  # ticks + blackout
    assert len(dmx_port.writes()) >= 2


def test_main_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "ArtNet" in captured.out


def test_setup_app_starts_http_when_enabled():
    cfg = _cfg_dmx_off()
    cfg["http"] = {"enabled": True, "bind": "127.0.0.1", "port": 0}
    poe_port = FakeSerial()
    app = setup_app(
        cfg, _quiet(),
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: pytest.fail("unused"),
    )
    assert app.http is not None
    assert app.static_info.code_version
    # Server isn't started yet (setup_app doesn't start anything)
    app.http.start()
    try:
        # Bind happened — port resolved to a real ephemeral value
        assert app.http.port > 0
    finally:
        app.http.stop()


def test_setup_app_installs_identify_at_startup_by_default():
    """With unit.identify_at_startup unset (defaults true), startup
    leaves an IdentifyOverride active so the rig has visible signal
    until ArtNet arrives."""
    from artnet_blaze.overrides import IdentifyOverride
    cfg = _cfg_dmx_off()
    # Add unit info; identify_at_startup unset → defaults to True.
    cfg["unit"] = {"name": "US1"}
    poe_port = FakeSerial()
    app = setup_app(
        cfg, _quiet(),
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: pytest.fail("unused"),
    )
    cur = app.controller.current()
    assert isinstance(cur, IdentifyOverride)
    assert cur.unit_name == "US1"


def test_setup_app_skips_startup_identify_when_disabled():
    cfg = _cfg_dmx_off()
    cfg["unit"] = {"name": "US1", "identify_at_startup": False}
    poe_port = FakeSerial()
    app = setup_app(
        cfg, _quiet(),
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: pytest.fail("unused"),
    )
    assert app.controller.current() is None


def test_setup_app_startup_identify_works_with_empty_name():
    """Empty unit name still installs identify (just no text painted)."""
    from artnet_blaze.overrides import IdentifyOverride
    cfg = _cfg_dmx_off()
    cfg["unit"] = {"name": ""}
    poe_port = FakeSerial()
    app = setup_app(
        cfg, _quiet(),
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: pytest.fail("unused"),
    )
    assert isinstance(app.controller.current(), IdentifyOverride)


def test_setup_app_records_dmx_firmware_via_probe():
    """The Enttec fw probe runs at port-open time. Inject a fake port that
    returns a scripted reply, and confirm static_info captures it."""
    import struct
    from artnet_blaze.dmx import (
        ENTTEC_END,
        ENTTEC_LABEL_GET_PARAMS,
        ENTTEC_START,
    )
    reply = (
        bytes([ENTTEC_START, ENTTEC_LABEL_GET_PARAMS])
        + struct.pack("<H", 5)
        + bytes([0x10, 0x02, 9, 1, 40, ENTTEC_END])
    )

    class _PortWithReply(FakeSerial):
        def __init__(self):
            super().__init__()
            self._replies = [reply]
        def reset_input_buffer(self): pass
        def read(self, n=1):
            return self._replies.pop(0) if self._replies else b""

    cfg = _cfg_dmx_on()
    poe_port = FakeSerial()
    dmx_port = _PortWithReply()

    app = setup_app(
        cfg, _quiet(),
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: dmx_port,
    )

    assert app.static_info.dmx_firmware == "fw 2.16"
    assert app.static_info.dmx_protocol == "enttec_pro"
    assert app.static_info.dmx_dongle == "fake-dmx"


def test_main_runs_full_lifecycle_via_config_file(tmp_path):
    """Drive `main()` through argparse + config file load + thread startup,
    then SIGTERM ourselves to exercise the real signal handler path."""
    import os
    import signal as signal_mod
    import threading

    poe_port, dmx_port = FakeSerial(), FakeSerial()

    cfg = _cfg_dmx_on()
    cfg["logging"]["stats_interval_s"] = 60  # don't spam during test
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg))

    # SIGTERM ourselves once main() is past setup_app and blocking on
    # join(). The Enttec firmware probe at startup uses a 0.5s timeout
    # against FakeSerial (which never replies), so we wait well past it.
    def kill_soon():
        time.sleep(1.0)
        os.kill(os.getpid(), signal_mod.SIGTERM)

    threading.Thread(target=kill_soon, daemon=True).start()
    rc = main(
        ["-c", str(p)],
        open_poe=lambda cfg: poe_port,
        open_dmx=lambda cfg: dmx_port,
    )
    assert rc == 0
    # The blackout-on-shutdown path appended a final write to each port.
    assert ("close",) in poe_port.events
    assert ("close",) in dmx_port.events
