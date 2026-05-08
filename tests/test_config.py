"""Config loading + validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from artnet_blaze.config import (
    DEFAULT_CONFIG,
    build_fixtures,
    build_strips,
    load_config,
)


def test_load_config_no_path_returns_defaults():
    cfg = load_config(None)
    assert cfg["artnet"]["bind"] == "0.0.0.0"
    assert cfg["dmx"]["enabled"] is False
    assert len(cfg["strips"]) == 8


def test_load_config_merges_user_over_defaults(tmp_path: Path):
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({
        "artnet": {"bind": "10.0.0.5"},
        "dmx": {"enabled": True, "device": "/dev/ttyUSB1"},
    }))
    cfg = load_config(p)
    # User override applied
    assert cfg["artnet"]["bind"] == "10.0.0.5"
    assert cfg["dmx"]["enabled"] is True
    assert cfg["dmx"]["device"] == "/dev/ttyUSB1"
    # Default fields preserved on merged dicts
    assert cfg["dmx"]["protocol"] == "enttec_pro"


def test_load_config_does_not_mutate_default():
    cfg = load_config(None)
    cfg["artnet"]["bind"] = "1.2.3.4"
    cfg2 = load_config(None)
    assert cfg2["artnet"]["bind"] == "0.0.0.0"
    # And the module-level default itself is unchanged
    assert DEFAULT_CONFIG["artnet"]["bind"] == "0.0.0.0"


def test_build_strips_accepts_valid():
    strips = build_strips([
        {"poe_channel": 0, "universe": 0, "offset": 0, "pixel_count": 64},
    ])
    assert strips[0].pixel_count == 64
    assert strips[0].row is None
    assert strips[0].side is None


def test_build_strips_accepts_row_and_side():
    strips = build_strips([
        {"poe_channel": 0, "universe": 0, "offset": 0, "pixel_count": 64,
         "row": 1, "side": "SR"},
        {"poe_channel": 1, "universe": 0, "offset": 192, "pixel_count": 64,
         "row": 1, "side": "SL"},
    ])
    assert strips[0].row == 1
    assert strips[0].side == "SR"
    assert strips[1].side == "SL"


def test_build_strips_rejects_invalid_side():
    with pytest.raises(ValueError):
        build_strips([{
            "poe_channel": 0, "universe": 0, "offset": 0, "pixel_count": 64,
            "side": "MIDDLE",
        }])


def test_build_strips_rejects_zero_or_negative_row():
    with pytest.raises(ValueError):
        build_strips([{
            "poe_channel": 0, "universe": 0, "offset": 0, "pixel_count": 64,
            "row": 0,
        }])


def test_default_config_has_unit_block():
    cfg = load_config(None)
    assert "unit" in cfg
    assert "name" in cfg["unit"]


def test_build_strips_rejects_bad_channel():
    with pytest.raises(ValueError):
        build_strips([
            {"poe_channel": 9, "universe": 0, "offset": 0, "pixel_count": 64},
        ])


def test_build_strips_rejects_universe_overrun():
    with pytest.raises(ValueError):
        # 200 pixels × 3 = 600 bytes, doesn't fit in 512
        build_strips([
            {"poe_channel": 0, "universe": 0, "offset": 0, "pixel_count": 200},
        ])


def test_build_fixtures_accepts_valid():
    fxs = build_fixtures([
        {"universe": 0, "offset": 384, "dmx_start": 1, "length": 24},
    ])
    assert fxs[0].dmx_start == 1
    assert fxs[0].length == 24


@pytest.mark.parametrize("bad", [
    {"universe": 0, "offset": 0,   "dmx_start": 0,   "length": 8},   # dmx_start < 1
    {"universe": 0, "offset": 0,   "dmx_start": 513, "length": 1},   # dmx_start > 512
    {"universe": 0, "offset": 0,   "dmx_start": 510, "length": 10},  # overruns wire
    {"universe": 0, "offset": 510, "dmx_start": 1,   "length": 10},  # overruns universe
    {"universe": 0, "offset": 0,   "dmx_start": 1,   "length": 0},   # zero length
])
def test_build_fixtures_rejects_invalid(bad):
    with pytest.raises(ValueError):
        build_fixtures([bad])


def test_build_fixtures_accepts_name_and_render():
    fxs = build_fixtures([{
        "universe": 4, "offset": 0, "dmx_start": 1, "length": 26,
        "name": "bar SR",
        "render": {"kind": "rgb_bar", "sections": 8,
                   "intensity_at": 24, "strobe_at": 25},
    }])
    assert fxs[0].name == "bar SR"
    assert fxs[0].render == {
        "kind": "rgb_bar", "sections": 8,
        "intensity_at": 24, "strobe_at": 25,
    }


def test_build_fixtures_render_defaults_to_raw():
    fxs = build_fixtures([{
        "universe": 0, "offset": 0, "dmx_start": 1, "length": 4,
    }])
    assert fxs[0].render is None
    assert fxs[0].name == ""


@pytest.mark.parametrize("bad_render", [
    {"kind": "spaceship"},                              # unknown kind
    {"kind": "rgb_bar", "sections": 0},                 # zero sections
    {"kind": "rgb_bar", "sections": -1},                # negative sections
    {"kind": "rgb_bar", "sections": 100},               # sections × 3 > length
    {"kind": "rgb_bar", "intensity_at": 99},            # offset out of range
    {"kind": "rgb_bar", "strobe_at": -1},               # negative offset
])
def test_build_fixtures_rejects_bad_render(bad_render):
    with pytest.raises(ValueError):
        build_fixtures([{
            "universe": 0, "offset": 0, "dmx_start": 1, "length": 26,
            "render": bad_render,
        }])


def test_build_fixtures_rejects_render_not_dict():
    with pytest.raises(ValueError):
        build_fixtures([{
            "universe": 0, "offset": 0, "dmx_start": 1, "length": 4,
            "render": "rgb_bar",  # must be dict
        }])
