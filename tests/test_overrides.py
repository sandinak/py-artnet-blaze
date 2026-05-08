"""UniformByteOverride + IdentifyOverride: layout + protocol."""

from __future__ import annotations

import pytest

from artnet_blaze.dmx import DmxFixture
from artnet_blaze.overrides import (
    GLYPH_HEIGHT,
    GLYPH_WIDTH,
    NAME_COLOR,
    SL_LINE_COLOR,
    SL_LINE_WIDTH,
    STAIRCASE_COLOR,
    IdentifyOverride,
    UniformByteOverride,
    make_uniform,
    render_text,
)
from artnet_blaze.poe import StripMapping


# ── render_text ────────────────────────────────────────────


def test_render_text_returns_4_rows():
    bm = render_text("U")
    assert len(bm) == GLYPH_HEIGHT
    assert all(len(r) == GLYPH_WIDTH for r in bm)


def test_render_text_inserts_gap_between_chars():
    bm = render_text("US")
    # 2 chars * 3 wide + 1 gap = 7 cols
    assert all(len(r) == GLYPH_WIDTH * 2 + 1 for r in bm)
    # The gap column (index 3) should be off in every row.
    for r in bm:
        assert r[3] is False


def test_render_text_unknown_char_is_blank():
    bm = render_text("@")
    # All cells off — unknown char renders as space
    assert all(not cell for row in bm for cell in row)


def test_render_text_uppercases_input():
    assert render_text("us1") == render_text("US1")


# ── UniformByteOverride ─────────────────────────────────────


def test_uniform_strip_pixels_fills_all_bytes():
    o = UniformByteOverride(0x80)
    s = StripMapping(poe_channel=0, universe=0, offset=0, pixel_count=4)
    out = o.strip_pixels(s)
    assert out == b"\x80" * 12


def test_uniform_dmx_values_fills_length_bytes():
    o = UniformByteOverride(0xFF)
    fx = DmxFixture(universe=0, offset=0, dmx_start=1, length=10)
    assert o.dmx_values(fx) == b"\xFF" * 10


def test_uniform_info_carries_value():
    assert UniformByteOverride(0x42).info() == {"value": 0x42}


def test_make_uniform_validates_range():
    with pytest.raises(ValueError):
        make_uniform(-1)
    with pytest.raises(ValueError):
        make_uniform(256)


# ── IdentifyOverride ────────────────────────────────────────


def _step_strips(rows: int = 4, leds: int = 64) -> list[StripMapping]:
    """Build a typical step layout: `rows` rows × 2 strips (SR/SL)."""
    out = []
    ch = 0
    for r in range(1, rows + 1):
        out.append(StripMapping(
            poe_channel=ch, universe=r-1, offset=0,
            pixel_count=leds, row=r, side="SR",
        ))
        ch += 1
        out.append(StripMapping(
            poe_channel=ch, universe=r-1, offset=192,
            pixel_count=leds, row=r, side="SL",
        ))
        ch += 1
    return out


def test_identify_strip_pixels_length_matches_strip():
    strips = _step_strips()
    o = IdentifyOverride(unit_name="US1", strips=strips)
    for s in strips:
        out = o.strip_pixels(s)
        assert len(out) == s.pixel_count * 3


def _triples(buf):
    return [(buf[i*3], buf[i*3+1], buf[i*3+2]) for i in range(len(buf) // 3)]


def test_identify_staircase_grows_per_visual_row():
    """8 strips → 8 visual rows. Visual row N has N staircase pixels."""
    strips = _step_strips(rows=4, leds=64)  # 4 config rows × 2 sides = 8 strips
    o = IdentifyOverride(unit_name="", strips=strips)
    # Visual order: (row 1 SR), (row 1 SL), (row 2 SR), (row 2 SL), …
    visual = sorted(strips, key=lambda s: (s.row, 0 if s.side == "SR" else 1))
    for visual_idx, strip in enumerate(visual):
        n = visual_idx + 1
        out = _triples(o.strip_pixels(strip))
        for i in range(n):
            assert out[i] == STAIRCASE_COLOR, \
                f"visual row {n} (ch {strip.poe_channel}) pixel {i} not amber"
        # Pixel n (just past the staircase) should NOT be amber.
        if n < strip.pixel_count - SL_LINE_WIDTH:
            assert out[n] != STAIRCASE_COLOR


def test_identify_sl_tip_paints_rightmost_4_pixels_on_every_strip():
    strips = _step_strips(rows=4, leds=64)
    o = IdentifyOverride(unit_name="", strips=strips)
    for s in strips:
        out = _triples(o.strip_pixels(s))
        for i in range(s.pixel_count - SL_LINE_WIDTH, s.pixel_count):
            assert out[i] == SL_LINE_COLOR, \
                f"ch {s.poe_channel} pixel {i} not white"


def test_identify_text_spans_all_strips_vertically():
    """Each strip shows one horizontal slice of the name. With 8 visual
    rows and a 4-row font, the slices stack into the full glyph."""
    strips = _step_strips(rows=4, leds=64)
    o = IdentifyOverride(unit_name="US1", strips=strips)
    # Every strip should have at least one NAME_COLOR pixel — "US1"
    # has lit cells in every row of the 4-tall font, and each font row
    # is mapped to 2 visual rows under the 4→8 scaling.
    visual = sorted(strips, key=lambda s: (s.row, 0 if s.side == "SR" else 1))
    for s in visual:
        triples = _triples(o.strip_pixels(s))
        assert NAME_COLOR in triples, \
            f"ch {s.poe_channel} has no name slice"


def test_identify_text_slice_assignment_doubles_rows():
    """Each font row of a 4-tall font lands on 2 consecutive visual rows
    when the unit has 8 strips. Strip pairs (0,1), (2,3), (4,5), (6,7)
    by visual order share the same set of NAME_COLOR positions."""
    strips = _step_strips(rows=4, leds=64)
    o = IdentifyOverride(unit_name="US1", strips=strips)
    visual = sorted(strips, key=lambda s: (s.row, 0 if s.side == "SR" else 1))

    def name_positions(strip):
        triples = _triples(o.strip_pixels(strip))
        # Mid-region only (skip staircase + tip) to avoid false positives.
        return {i for i, t in enumerate(triples)
                if t == NAME_COLOR}

    # Pairs (0,1), (2,3), (4,5), (6,7) sharing slices.
    for pair_start in (0, 2, 4, 6):
        a = name_positions(visual[pair_start])
        b = name_positions(visual[pair_start + 1])
        assert a == b, \
            f"strips {pair_start} and {pair_start+1} should share a slice"


def test_identify_strip_without_row_metadata_goes_dark():
    s_with = StripMapping(poe_channel=0, universe=0, offset=0,
                          pixel_count=8, row=1, side="SR")
    s_without = StripMapping(poe_channel=1, universe=0, offset=24,
                             pixel_count=8)  # no row/side
    o = IdentifyOverride(unit_name="X", strips=[s_with, s_without])
    out = o.strip_pixels(s_without)
    assert out == bytes(8 * 3)


def test_identify_dmx_values_are_zero():
    """During identify the DMX bars stay dark — focus is on the LEDs."""
    o = IdentifyOverride(unit_name="US1", strips=_step_strips())
    fx = DmxFixture(universe=4, offset=0, dmx_start=1, length=26)
    assert o.dmx_values(fx) == bytes(26)


def test_identify_handles_empty_unit_name():
    """Empty name → no text rendered, but staircase + SL line still work."""
    strips = _step_strips()
    o = IdentifyOverride(unit_name="", strips=strips)
    sr1 = next(s for s in strips if s.row == 1 and s.side == "SR")
    out = o.strip_pixels(sr1)
    # Pixel 0 should still be STAIRCASE_COLOR
    assert (out[0], out[1], out[2]) == STAIRCASE_COLOR


def test_identify_info_carries_unit_name():
    o = IdentifyOverride(unit_name="US3", strips=[])
    assert o.info() == {"unit": "US3"}
    assert o.kind == "identify"


def test_identify_with_single_strip_per_row():
    """A unit with just one strip per row (no SL pair) — staircase still
    paints; SL line lands on the rightmost pixels of that single strip."""
    strips = [
        StripMapping(poe_channel=0, universe=0, offset=0,
                     pixel_count=20, row=1, side="SR"),
    ]
    o = IdentifyOverride(unit_name="A", strips=strips)
    out = o.strip_pixels(strips[0])
    # Staircase: pixel 0
    assert (out[0], out[1], out[2]) == STAIRCASE_COLOR
    # SL line: pixels 16..19
    for i in range(16, 20):
        assert (out[i*3], out[i*3+1], out[i*3+2]) == SL_LINE_COLOR