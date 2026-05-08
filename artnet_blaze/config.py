"""Config loading + validation for ArtNet, POE, DMX."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from .dmx import DMX_SLOT_COUNT, DmxFixture
from .poe import StripMapping


DEFAULT_CONFIG: dict = {
    "artnet": {"bind": "0.0.0.0"},
    "serial": {"device": "/dev/serial0", "baudrate": 2_000_000},
    "bridge": {"fps": 50},
    # Identifies the physical unit on the HTTP test panel and in the
    # identify pattern (paints `name` across the LED rows).
    # `identify_at_startup`: paint the identify pattern as soon as the
    # daemon starts, holding it until live ArtNet arrives. Operators
    # can disable for show day to avoid the brief flash on restart.
    "unit": {"name": "", "identify_at_startup": True},
    # Evolution step: 4 universes, 2 strips per universe, 64 LEDs/strip.
    # 64 * 3 = 192 bytes per strip, 384 bytes per universe = leaves 128
    # bytes free per universe for piggybacked DMX fixtures if desired.
    "strips": [
        {"poe_channel": 0, "universe": 0, "offset": 0,   "pixel_count": 64, "row": 1, "side": "SR"},
        {"poe_channel": 1, "universe": 0, "offset": 192, "pixel_count": 64, "row": 1, "side": "SL"},
        {"poe_channel": 2, "universe": 1, "offset": 0,   "pixel_count": 64, "row": 2, "side": "SR"},
        {"poe_channel": 3, "universe": 1, "offset": 192, "pixel_count": 64, "row": 2, "side": "SL"},
        {"poe_channel": 4, "universe": 2, "offset": 0,   "pixel_count": 64, "row": 3, "side": "SR"},
        {"poe_channel": 5, "universe": 2, "offset": 192, "pixel_count": 64, "row": 3, "side": "SL"},
        {"poe_channel": 6, "universe": 3, "offset": 0,   "pixel_count": 64, "row": 4, "side": "SR"},
        {"poe_channel": 7, "universe": 3, "offset": 192, "pixel_count": 64, "row": 4, "side": "SL"},
    ],
    "dmx": {
        "enabled": False,
        "device": "/dev/ttyUSB0",
        "protocol": "enttec_pro",
        "fps": 40,
        "fixtures": [],
    },
    "http": {
        "enabled": True,
        "bind": "0.0.0.0",
        "port": 8080,
    },
    "logging": {"level": "INFO", "stats_interval_s": 10},
}


def load_config(path: Optional[Path]) -> dict:
    """Deep-merge user config (one level) over DEFAULT_CONFIG."""
    if path is None:
        return _deep_copy_default()
    with open(path) as f:
        user = yaml.safe_load(f) or {}
    merged = _deep_copy_default()
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def _deep_copy_default() -> dict:
    # One level of nesting is enough for our config shape.
    return {
        k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
        for k, v in DEFAULT_CONFIG.items()
    }


_VALID_SIDES = ("SR", "SL")


def build_strips(raw: list[dict]) -> list[StripMapping]:
    out = []
    for s in raw:
        side = s.get("side")
        if side is not None:
            if side not in _VALID_SIDES:
                raise ValueError(
                    f"strip side must be one of {_VALID_SIDES}, got {side!r}"
                )
        row = s.get("row")
        if row is not None:
            row = int(row)
            if row < 1:
                raise ValueError(f"strip row must be >= 1, got {row}")
        m = StripMapping(
            poe_channel=int(s["poe_channel"]),
            universe=int(s["universe"]),
            offset=int(s["offset"]),
            pixel_count=int(s["pixel_count"]),
            row=row,
            side=side,
        )
        if not 0 <= m.poe_channel <= 7:
            raise ValueError(f"poe_channel out of range: {m.poe_channel}")
        if m.offset < 0 or m.pixel_count < 0:
            raise ValueError(f"negative offset/pixel_count on ch{m.poe_channel}")
        if m.offset + m.pixel_count * 3 > 512:
            raise ValueError(
                f"strip on ch{m.poe_channel} overruns universe "
                f"{m.universe}: offset={m.offset} pixels={m.pixel_count}"
            )
        out.append(m)
    return out


_VALID_RENDER_KINDS = ("raw", "rgb_bar")


def _validate_render(render: dict, length: int) -> dict:
    if not isinstance(render, dict):
        raise ValueError(f"fixture render must be a dict, got {type(render).__name__}")
    kind = render.get("kind", "raw")
    if kind not in _VALID_RENDER_KINDS:
        raise ValueError(
            f"unknown render kind {kind!r}; must be one of {_VALID_RENDER_KINDS}"
        )
    if kind == "rgb_bar":
        sections = render.get("sections")
        if sections is not None:
            if not isinstance(sections, int) or sections <= 0:
                raise ValueError(f"render.sections must be positive int, got {sections!r}")
            if sections * 3 > length:
                raise ValueError(
                    f"render.sections={sections} needs {sections*3} bytes "
                    f"but fixture length is only {length}"
                )
        for k in ("intensity_at", "strobe_at"):
            if k in render and render[k] is not None:
                idx = render[k]
                if not isinstance(idx, int) or not 0 <= idx < length:
                    raise ValueError(
                        f"render.{k}={idx!r} out of range for length {length}"
                    )
    return render


def build_fixtures(raw: list[dict]) -> list[DmxFixture]:
    out = []
    for f in raw:
        length = int(f["length"])
        render = f.get("render")
        if render is not None:
            render = _validate_render(render, length)
        fx = DmxFixture(
            universe=int(f["universe"]),
            offset=int(f["offset"]),
            dmx_start=int(f["dmx_start"]),
            length=length,
            name=str(f.get("name", "")),
            render=render,
        )
        if fx.length <= 0:
            raise ValueError(f"fixture length must be > 0: {fx}")
        if fx.offset < 0 or fx.offset + fx.length > 512:
            raise ValueError(f"fixture source overruns universe: {fx}")
        if not 1 <= fx.dmx_start <= DMX_SLOT_COUNT:
            raise ValueError(f"dmx_start out of range (1..512): {fx}")
        if fx.dmx_start - 1 + fx.length > DMX_SLOT_COUNT:
            raise ValueError(f"fixture overruns DMX wire: {fx}")
        out.append(fx)
    return out
