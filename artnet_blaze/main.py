"""CLI entry: wire ArtNet receiver + sinks + HTTP test panel.

`main()` is the CLI shell. The wiring (open ports, build sinks, hook the
receiver, attach the test controller) lives in `setup_app()` so tests
can drive the assembly with fake serial ports without going through
argparse + signal handlers.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Optional

import serial  # pyserial

from . import __version__
from .artnet import ARTNET_PORT, ArtNetReceiver
from .config import build_fixtures, build_strips, load_config
from .controller import TestController
from .dmx import (
    PROTOCOL_ENTTEC_PRO,
    PROTOCOL_OPEN_DMX,
    DmxSink,
    query_enttec_firmware,
)
from .http_api import HttpServerThread
from .overrides import IdentifyOverride
from .poe import PoeSink
from .readiness import evaluate_readiness
from .sink import Sink
from .status_led import StatusLedThread, make_led
from .sysinfo import StaticInfo


def _default_open_poe(cfg: dict):
    return serial.Serial(
        cfg["serial"]["device"],
        baudrate=int(cfg["serial"]["baudrate"]),
        timeout=0,
        write_timeout=0.1,
    )


def _default_open_dmx(cfg: dict):
    proto = cfg["dmx"]["protocol"]
    if proto == PROTOCOL_OPEN_DMX:
        return serial.Serial(
            cfg["dmx"]["device"],
            baudrate=250000,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
            timeout=0,
            write_timeout=0.1,
        )
    # Enttec Pro: short read timeout so we can synchronously query firmware.
    return serial.Serial(
        cfg["dmx"]["device"],
        baudrate=115200,
        timeout=0.5,
        write_timeout=0.1,
    )


@dataclasses.dataclass
class App:
    """Assembled-but-not-started bridge."""
    rx: ArtNetReceiver
    poe_sink: PoeSink
    dmx_sink: Optional[DmxSink]
    poe_port: object
    dmx_port: Optional[object]
    controller: TestController
    static_info: StaticInfo
    http: Optional[HttpServerThread] = None
    status_led: Optional[StatusLedThread] = None

    @property
    def sinks(self) -> list[Sink]:
        return [self.poe_sink] + ([self.dmx_sink] if self.dmx_sink else [])


def setup_app(
    cfg: dict,
    log: logging.Logger,
    open_poe=_default_open_poe,
    open_dmx=_default_open_dmx,
) -> App:
    """Build everything from cfg, opening ports via the given factories."""
    strips = build_strips(cfg["strips"])
    dmx_cfg = cfg.get("dmx", {})
    dmx_enabled = bool(dmx_cfg.get("enabled"))
    fixtures = build_fixtures(dmx_cfg.get("fixtures", [])) if dmx_enabled else []

    universes: set[int] = {s.universe for s in strips}
    universes |= {f.universe for f in fixtures}

    rx = ArtNetReceiver(
        cfg["artnet"]["bind"],
        universes,
        log,
        port=int(cfg["artnet"].get("port", ARTNET_PORT)),
    )

    controller = TestController(rx)

    poe_port = open_poe(cfg)
    log.info(
        "POE serial open: %s @ %d baud",
        cfg["serial"]["device"], cfg["serial"]["baudrate"],
    )
    poe_sink = PoeSink(
        rx, poe_port, strips, float(cfg["bridge"]["fps"]), log,
        controller=controller,
    )

    static_info = StaticInfo.collect(__version__)
    unit_cfg = cfg.get("unit", {})
    static_info.unit_name = str(unit_cfg.get("name", ""))

    dmx_sink: Optional[DmxSink] = None
    dmx_port = None
    if dmx_enabled and fixtures:
        dmx_port = open_dmx(cfg)
        log.info(
            "DMX serial open: %s protocol=%s",
            dmx_cfg["device"], dmx_cfg["protocol"],
        )
        proto = dmx_cfg["protocol"]
        static_info.dmx_dongle = dmx_cfg["device"]
        static_info.dmx_protocol = proto
        if proto == PROTOCOL_ENTTEC_PRO:
            fw = query_enttec_firmware(dmx_port, log)
            static_info.dmx_firmware = fw or "query failed"
        else:
            static_info.dmx_firmware = "n/a (open_dmx — no inquiry)"
        dmx_sink = DmxSink(
            rx, dmx_port, fixtures,
            protocol=proto,
            fps=float(dmx_cfg.get("fps", 40)),
            log=log,
            controller=controller,
        )
    elif dmx_enabled:
        log.warning("dmx.enabled=true but no fixtures configured; skipping DMX sink")

    if unit_cfg.get("identify_at_startup", True):
        controller.set_override(
            IdentifyOverride(unit_name=static_info.unit_name, strips=strips)
        )
        log.info(
            "identify pattern installed at startup (unit=%r); "
            "will yield to live ArtNet once it arrives + min_hold elapses",
            static_info.unit_name,
        )

    app = App(
        rx=rx, poe_sink=poe_sink, dmx_sink=dmx_sink,
        poe_port=poe_port, dmx_port=dmx_port,
        controller=controller, static_info=static_info,
    )

    # Status LED — opt-in, polls readiness every poll_interval_s.
    sled_cfg = cfg.get("status_led", {})
    artnet_window = float(sled_cfg.get("artnet_active_window_s", 2.0))
    if sled_cfg.get("enabled", False):
        led = make_led(
            int(sled_cfg.get("red_pin", 17)),
            int(sled_cfg.get("green_pin", 27)),
            int(sled_cfg.get("blue_pin", 22)),
            bool(sled_cfg.get("common_anode", False)),
            log,
        )
        app.status_led = StatusLedThread(
            led=led,
            evaluator=lambda: evaluate_readiness(app, artnet_window).state,
            log=log,
            poll_interval_s=float(sled_cfg.get("poll_interval_s", 0.5)),
            debounce_ticks=int(sled_cfg.get("debounce_ticks", 2)),
        )
        log.info(
            "status LED enabled (R=%s G=%s B=%s)",
            sled_cfg.get("red_pin", 17),
            sled_cfg.get("green_pin", 27),
            sled_cfg.get("blue_pin", 22),
        )

    # HTTP server constructed last so it can hold closures over the
    # fully-assembled app (readiness predicate, LED for the test route).
    http_cfg = cfg.get("http", {})
    if http_cfg.get("enabled", True):
        app.http = HttpServerThread(
            bind=http_cfg.get("bind", "0.0.0.0"),
            port=int(http_cfg.get("port", 8080)),
            static=static_info,
            controller=controller,
            receiver=rx,
            strips=strips,
            fixtures=fixtures,
            log=log,
            readiness_fn=lambda: evaluate_readiness(app, artnet_window),
            status_led=app.status_led,
        )

    return app


def stats_line(app: App) -> str:
    parts = [
        f"rx={app.rx.frames_rx}",
        f"stray={app.rx.stray_rx}",
        f"poe_tx={app.poe_sink.frames_tx}",
        f"poe_err={app.poe_sink.tx_errors}",
        f"poe_late={app.poe_sink.late_ticks}",
    ]
    if app.dmx_sink is not None:
        parts += [
            f"dmx_tx={app.dmx_sink.frames_tx}",
            f"dmx_err={app.dmx_sink.tx_errors}",
            f"dmx_late={app.dmx_sink.late_ticks}",
        ]
    return " ".join(parts)


def shutdown_app(app: App, log: logging.Logger) -> None:
    """Stop everything and close ports. Safe to call more than once."""
    if app.http is not None:
        app.http.stop()
    if app.status_led is not None:
        app.status_led.stop()
    for s in app.sinks:
        s.stop()
    app.rx.stop()
    for s in app.sinks:
        s.join(timeout=2.0)
    if app.status_led is not None:
        app.status_led.join(timeout=2.0)
    for port in (app.poe_port, app.dmx_port):
        if port is None:
            continue
        try:
            port.close()
        except Exception as e:
            log.warning("port close failed: %s", e)


def main(argv: list[str], open_poe=None, open_dmx=None) -> int:
    ap = argparse.ArgumentParser(
        description="ArtNet → Pixelblaze POE + USB DMX bridge",
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

    app = setup_app(
        cfg, log,
        open_poe=open_poe or _default_open_poe,
        open_dmx=open_dmx or _default_open_dmx,
    )

    app.rx.start()
    for s in app.sinks:
        s.start()
    if app.http is not None:
        app.http.start()
    if app.status_led is not None:
        app.status_led.start()

    stop_event = threading.Event()

    def handle_signal(signum, _frame):
        log.info("signal %d received, shutting down", signum)
        for s in app.sinks:
            s.stop()
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    def stats_loop():
        interval = float(cfg["logging"]["stats_interval_s"])
        while not stop_event.is_set():
            time.sleep(interval)
            log.info(stats_line(app))

    threading.Thread(target=stats_loop, daemon=True).start()

    try:
        for s in app.sinks:
            s.join()
    finally:
        shutdown_app(app, log)
        log.info("exited")
    return 0
