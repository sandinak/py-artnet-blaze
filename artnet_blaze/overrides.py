"""Test-pattern overrides.

An `Override` describes how the rig should be painted while a test
pattern is active. Overrides know how to render themselves into both
POE strip pixel data and DMX channel data, so the controller can flip
between flat colors (`UniformByteOverride`) and structured patterns
(`IdentifyOverride`) without the sinks needing to know which is which.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Protocol

if TYPE_CHECKING:
    from .dmx import DmxFixture
    from .poe import StripMapping


class Override(Protocol):
    """Common interface for any test-pattern override."""

    kind: str
    """Short tag used in the JSON status payload (e.g. "uniform", "identify")."""

    def strip_pixels(self, strip: "StripMapping") -> bytes:
        """Return `pixel_count*3` bytes of RGB pixel data for one strip."""
        ...

    def dmx_values(self, fixture: "DmxFixture") -> bytes:
        """Return `length` bytes of channel data for one DMX fixture."""
        ...

    def info(self) -> dict:
        """Status fields for the HTTP panel (kind-specific)."""
        ...


@dataclass
class UniformByteOverride:
    """Every output byte is set to a single value (0..255)."""

    value: int
    kind: str = "uniform"

    def strip_pixels(self, strip: "StripMapping") -> bytes:
        return bytes([self.value]) * (strip.pixel_count * 3)

    def dmx_values(self, fixture: "DmxFixture") -> bytes:
        return bytes([self.value]) * fixture.length

    def info(self) -> dict:
        return {"value": self.value}


# ── Identify pattern ─────────────────────────────────────────────

# 3-wide × 4-tall bitmap font. Tall enough to use the full 4-row step
# geometry; small enough to fit "US1"-style names with comfortable
# inter-character spacing on a 128-LED-wide row.
#
# Each glyph is a list of 4 strings (top → bottom). '#' = lit, ' ' = off.
_GLYPHS_4x3: dict[str, list[str]] = {
    " ": ["   ",
          "   ",
          "   ",
          "   "],
    "0": ["###",
          "# #",
          "# #",
          "###"],
    "1": [" # ",
          "## ",
          " # ",
          "###"],
    "2": ["## ",
          "  #",
          " # ",
          "###"],
    "3": ["## ",
          " ##",
          "  #",
          "## "],
    "4": ["# #",
          "###",
          "  #",
          "  #"],
    "5": ["###",
          "## ",
          "  #",
          "## "],
    "6": [" ##",
          "#  ",
          "###",
          "###"],
    "7": ["###",
          "  #",
          " # ",
          "#  "],
    "8": ["###",
          " # ",
          "###",
          "###"],
    "9": ["###",
          "###",
          "  #",
          "## "],
    "A": [" # ",
          "# #",
          "###",
          "# #"],
    "B": ["## ",
          "###",
          "# #",
          "## "],
    "C": [" ##",
          "#  ",
          "#  ",
          " ##"],
    "D": ["## ",
          "# #",
          "# #",
          "## "],
    "E": ["###",
          "## ",
          "#  ",
          "###"],
    "F": ["###",
          "## ",
          "#  ",
          "#  "],
    "G": [" ##",
          "#  ",
          "# #",
          " ##"],
    "H": ["# #",
          "###",
          "# #",
          "# #"],
    "I": ["###",
          " # ",
          " # ",
          "###"],
    "J": ["  #",
          "  #",
          "# #",
          "###"],
    "K": ["# #",
          "## ",
          "## ",
          "# #"],
    "L": ["#  ",
          "#  ",
          "#  ",
          "###"],
    "M": ["# #",
          "###",
          "# #",
          "# #"],
    "N": ["# #",
          "###",
          "###",
          "# #"],
    "O": ["###",
          "# #",
          "# #",
          "###"],
    "P": ["## ",
          "###",
          "#  ",
          "#  "],
    "Q": ["###",
          "# #",
          "## ",
          "###"],
    "R": ["## ",
          "###",
          "## ",
          "# #"],
    "S": [" ##",
          "## ",
          " ##",
          "## "],
    "T": ["###",
          " # ",
          " # ",
          " # "],
    "U": ["# #",
          "# #",
          "# #",
          "###"],
    "V": ["# #",
          "# #",
          "# #",
          " # "],
    "W": ["# #",
          "# #",
          "###",
          "# #"],
    "X": ["# #",
          " # ",
          " # ",
          "# #"],
    "Y": ["# #",
          "# #",
          " # ",
          " # "],
    "Z": ["###",
          " ##",
          "## ",
          "###"],
    "-": ["   ",
          "###",
          "   ",
          "   "],
}

GLYPH_HEIGHT = 4
GLYPH_WIDTH = 3
GLYPH_GAP = 1


def render_text(text: str) -> list[list[bool]]:
    """Render uppercase text into a 4-row bitmap.

    Returns a 4-row list, each row a list of bools (True = lit). Width
    is `len(text) * (GLYPH_WIDTH + GLYPH_GAP) - GLYPH_GAP` for non-empty
    input. Unknown characters render as a space.
    """
    text = text.upper()
    rows: list[list[bool]] = [[] for _ in range(GLYPH_HEIGHT)]
    for i, ch in enumerate(text):
        glyph = _GLYPHS_4x3.get(ch, _GLYPHS_4x3[" "])
        for r in range(GLYPH_HEIGHT):
            for c in range(GLYPH_WIDTH):
                rows[r].append(glyph[r][c] == "#")
        if i < len(text) - 1:
            for r in range(GLYPH_HEIGHT):
                for _ in range(GLYPH_GAP):
                    rows[r].append(False)
    return rows


# ── Identify override ────────────────────────────────────────────

# Colors used by IdentifyOverride. Tuples of (R, G, B) bytes 0..255.
STAIRCASE_COLOR = (255, 110, 0)   # amber — high contrast, hard to mistake
SL_LINE_COLOR = (255, 255, 255)   # white tip on stage-left edge
NAME_COLOR = (60, 60, 60)         # dim grey label
SL_LINE_WIDTH = 4                 # last N pixels of each row's SL strip


@dataclass
class IdentifyOverride:
    """Diagnostic identify pattern.

    Each strip is treated as one visual row of the unit. Strips are
    ordered for visual purposes by `(row, side)` — within the same
    config row, SR comes before SL — so a unit configured with 4 rows
    × 2 sides becomes 8 visual rows numbered 1..8.

    For each visual row N (1-based):
      * Staircase: leftmost N pixels painted amber. 1 pixel on row 1,
        2 on row 2, … all the way to N on row N. Confirms row order.
      * SL tip: rightmost `SL_LINE_WIDTH` pixels painted white.
        Confirms each strip is reaching its far edge.
      * Name slice: one horizontal slice of the unit name, drawn from
        a 4-row bitmap font scaled vertically to span all visual rows.
        With 8 visual rows and a 4-tall font, each font row appears on
        2 adjacent strips, giving a 2×-stretched but readable label.

    Strips without `row` metadata are skipped (their LEDs go dark).
    """

    unit_name: str
    strips: list["StripMapping"]
    kind: str = "identify"

    def __post_init__(self) -> None:
        self._patterns: dict[int, bytes] = {}
        self._build()

    def _visual_strips(self) -> list["StripMapping"]:
        """Strips with row metadata, sorted into visual top-to-bottom order."""
        def _side_key(s):
            return {"SR": 0, "SL": 1}.get(getattr(s, "side", None) or "", 2)
        return sorted(
            (s for s in self.strips if getattr(s, "row", None) is not None),
            key=lambda s: (s.row, _side_key(s)),
        )

    def _build(self) -> None:
        visual = self._visual_strips()
        total_rows = len(visual)
        if total_rows == 0:
            return

        text_bitmap = render_text(self.unit_name) if self.unit_name else None
        native_h = len(text_bitmap) if text_bitmap else 0
        text_w = len(text_bitmap[0]) if text_bitmap and text_bitmap[0] else 0

        for visual_idx, strip in enumerate(visual):
            visual_row = visual_idx + 1  # 1-based count
            pixels: list[tuple[int, int, int]] = [(0, 0, 0)] * strip.pixel_count

            # Staircase: leftmost visual_row pixels.
            for i in range(min(visual_row, strip.pixel_count)):
                pixels[i] = STAIRCASE_COLOR

            # SL tip: rightmost SL_LINE_WIDTH pixels.
            for i in range(max(0, strip.pixel_count - SL_LINE_WIDTH),
                           strip.pixel_count):
                pixels[i] = SL_LINE_COLOR

            # Name slice: this strip displays one horizontal row of the
            # bitmap font, scaled vertically across all visual rows.
            if text_bitmap and text_w and text_w <= strip.pixel_count:
                font_row = (visual_idx * native_h) // total_rows
                font_row = min(font_row, native_h - 1)
                slice_row = text_bitmap[font_row]
                start = (strip.pixel_count - text_w) // 2
                for i, on in enumerate(slice_row):
                    if not on:
                        continue
                    pos = start + i
                    # Don't overwrite staircase or SL tip.
                    if 0 <= pos < strip.pixel_count and pixels[pos] == (0, 0, 0):
                        pixels[pos] = NAME_COLOR

            buf = bytearray()
            for r, g, b in pixels:
                buf.extend((r, g, b))
            self._patterns[strip.poe_channel] = bytes(buf)

    # ── Override protocol ──────────────────────────────────

    def strip_pixels(self, strip: "StripMapping") -> bytes:
        cached = self._patterns.get(strip.poe_channel)
        if cached is not None:
            return cached
        # Strip not part of the unit (or no row metadata) → off.
        return bytes(strip.pixel_count * 3)

    def dmx_values(self, fixture: "DmxFixture") -> bytes:
        # DMX fixtures stay dark during identify so attention is on the
        # LED unit itself.
        return bytes(fixture.length)

    def info(self) -> dict:
        return {"unit": self.unit_name}


def make_uniform(value: int) -> UniformByteOverride:
    """Convenience constructor mirroring the legacy controller API."""
    if not 0 <= value <= 255:
        raise ValueError(f"value must be 0..255, got {value}")
    return UniformByteOverride(value=value)
