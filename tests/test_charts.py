"""The sparkline's chart rules, pinned.

`buildSparkline` in `static/js/app.js` draws both charts in the app (price
history in Insights, and per-server traffic). The decisions below are the ones
that are easy to undo by accident and invisible when they regress -- a dashed
grid looks fine, a second blue looks fine, dropping the table view looks fine.

The colour check is the validator's own math (OKLab / OKLCH / WCAG), inlined so
it runs in CI rather than living in a comment: the series colour must clear the
dark lightness band, the chroma floor, and 3:1 against the surface the chart
actually renders on -- ECOVibe's gray-800 panel, not a generic dark.
"""
import math
import os
import re

import pytest

APP_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "app.js"
)

# ECOVibe is dark-only (no `prefers-color-scheme`, no `dark:` variants anywhere),
# so there is one surface, and it is the panel the charts sit in: gray-800.
SURFACE = "#1e2939"
DARK_BAND = (0.48, 0.67)
CHROMA_FLOOR = 0.10
CONTRAST_MIN = 3.0


# ----- the validator's colour math -----


def _lin(hex_color):
    h = hex_color.lstrip("#")
    out = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return out


def _oklab(hex_color):
    r, g, b = _lin(hex_color)
    lc = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    mc = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    sc = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (
        0.2104542553 * lc + 0.7936177850 * mc - 0.0040720468 * sc,
        1.9779984951 * lc - 2.4285922050 * mc + 0.4505937099 * sc,
        0.0259040371 * lc + 0.7827717662 * mc - 0.8086757660 * sc,
    )


def _oklch(hex_color):
    L, a, b = _oklab(hex_color)
    return L, math.hypot(a, b)


def _contrast(a, b):
    def lum(h):
        r, g, bl = _lin(h)
        return 0.2126 * r + 0.7152 * g + 0.0722 * bl
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _sparkline_source():
    src = open(APP_JS).read()
    start = src.index("function buildSparkline(")
    # Ends at the next top-level declaration -- which is `async function init`,
    # so match either form rather than a bare "function".
    m = re.search(r"\n(?:async )?function ", src[start + 1:])
    assert m, "could not find the end of buildSparkline"
    return src[start:start + 1 + m.start()]


def _series_color():
    src = open(APP_JS).read()
    return re.search(r"const SPARK_SERIES = '(#[0-9a-fA-F]{6})'", src).group(1)


# ----- colour -----


def test_series_color_clears_the_dark_band_and_chroma_floor():
    L, C = _oklch(_series_color())
    assert DARK_BAND[0] <= L <= DARK_BAND[1], f"L={L:.3f} outside {DARK_BAND}"
    assert C >= CHROMA_FLOOR, f"C={C:.3f} below {CHROMA_FLOOR} (would read gray)"


def test_series_color_clears_contrast_against_the_panel_it_renders_on():
    ratio = _contrast(_series_color(), SURFACE)
    assert ratio >= CONTRAST_MIN, f"{ratio:.2f}:1 vs {SURFACE}, need {CONTRAST_MIN}:1"


def test_the_plot_uses_exactly_one_hue():
    """One series means one colour. An earlier version drew the line in blue-500
    and the dots in blue-400 -- a two-slot categorical palette encoding nothing,
    whose separation (ΔE 10.0) is below the normal-vision floor of 15.

    Colours come from the three named constants, so the check is that the plot
    references exactly one *series* constant alongside the two chrome ones, and
    that no raw hex was pasted back in beside them.
    """
    src = _sparkline_source()
    assert not re.search(r"#[0-9a-fA-F]{6}", src), "raw hex in the plot; use the constants"

    used = set(re.findall(r"SPARK_[A-Z]+", src))
    assert used == {"SPARK_SERIES", "SPARK_GRID", "SPARK_SURFACE"}, used
    # The mark colours -- line stroke, area fill, marker -- are all the one series hue.
    assert src.count("SPARK_SERIES") == 3
    # ...and the grid is chrome, never the series colour.
    assert 'stroke="${SPARK_GRID}"' in src


# ----- marks & chrome -----


def test_gridlines_are_solid():
    """Dashing reads as a threshold or a projection when it is only a grid."""
    assert "stroke-dasharray" not in _sparkline_source()


def test_the_line_keeps_its_width_when_the_svg_is_stretched():
    """preserveAspectRatio="none" is what lets the plot fill any container width,
    but it scales stroke width with it -- and it is why the old per-point
    `<circle>` dots rendered as ellipses at every width but 400px. The line needs
    non-scaling-stroke; round marks live in the HTML overlay instead."""
    src = _sparkline_source()
    assert 'preserveAspectRatio="none"' in src
    assert src.count("non-scaling-stroke") >= 3      # two gridlines + the line
    assert "<circle" not in src


# ----- interaction & accessibility -----


@pytest.mark.parametrize("key", ["ArrowRight", "ArrowLeft", "Home", "End", "Escape"])
def test_values_are_reachable_by_keyboard(key):
    """A tooltip must never be the only way to read a value."""
    src = _sparkline_source()
    assert "tabindex" in src
    assert key in src


def test_there_is_a_table_view():
    """The WCAG-clean twin: every value readable without hovering anything."""
    src = _sparkline_source()
    assert "Show data table" in src
    assert "<table" in src or "el('table'" in src


def test_hover_resolves_to_the_nearest_point_rather_than_a_dot():
    """The hit target is the whole plot, so there is no 7px dot to land on."""
    src = _sparkline_source()
    assert "pointermove" in src
    assert "nearest" in src
    assert "getBoundingClientRect" in src
