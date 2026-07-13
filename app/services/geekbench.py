"""Geekbench 6 scores for the CPUs that appear in OVH's ECO catalog.

Geekbench has no public API, so this is a hand-curated static table of
representative Geekbench 6 single- and multi-core scores, keyed by a
normalised ``"<brand> <model>"`` string that matches OVH's
``cpu.brand``/``cpu.model`` fields (case-insensitively).

Values are approximate medians drawn from public Geekbench Browser results and
should be spot-checked against https://browser.geekbench.com/search — they are
meant for *relative* comparison of servers, not exact benchmarking. Coverage
targets every CPU that appears in OVH's live ECO catalog (US/EU/CA), so
score-sorting in the UI orders all servers rather than dumping unscored ones to
the bottom. A couple of the oldest chips (Ivy Bridge-EP Xeon E5 v2) have sparse
Geekbench 6 data and their figures are best-effort estimates. If OVH introduces
a CPU not in this table, ``lookup`` returns ``None`` (no badge, sorts last) and
``catalog._build_product_specs`` logs a warning so the gap is noticed.

Scores are per physical CPU. A few OVH boxes are dual-socket (``cpu.number > 1``);
we still report the single-chip figure (Geekbench 6 multi-core does not scale
cleanly across sockets and 2P results are scarce).
"""
from typing import Any

# key: normalised "<brand> <model>"  ->  {"single": int, "multi": int}
# Grouped by family for readability. See module docstring for provenance.
GEEKBENCH6: dict[str, dict[str, int]] = {
    # --- AMD Ryzen (desktop Zen2/Zen3/Zen5) ---
    "amd ryzen 5 3600x": {"single": 1500, "multi": 6700},
    "amd ryzen 5 pro 3600": {"single": 1450, "multi": 6600},
    "amd ryzen 7 3800x": {"single": 1600, "multi": 8600},
    "amd ryzen 7 pro 3700": {"single": 1550, "multi": 8400},
    "amd ryzen 5 5600x": {"single": 2100, "multi": 9200},
    "amd ryzen 7 5800x": {"single": 2250, "multi": 11200},
    "amd ryzen 9 5900x": {"single": 2250, "multi": 14200},
    "amd ryzen 7 9700x": {"single": 3250, "multi": 15400},
    "amd ryzen 9 9900x": {"single": 3350, "multi": 19300},
    "amd ryzen 9 9950x": {"single": 3400, "multi": 21700},
    # --- AMD EPYC 4004 (AM5, Zen4 desktop-class) ---
    "amd epyc 4244p": {"single": 2500, "multi": 11500},
    "amd epyc 4344p": {"single": 2600, "multi": 14000},
    "amd epyc 4464p": {"single": 2600, "multi": 17500},
    "amd epyc 4584px": {"single": 2800, "multi": 18500},
    # --- AMD EPYC 9005 (Turin, Zen5 server) ---
    "amd epyc turin 9455": {"single": 2700, "multi": 21200},
    # --- AMD EPYC 8004 (Zen4c) ---
    "amd epyc 8224p": {"single": 1450, "multi": 12500},
    # --- AMD EPYC 7002/7003 (Zen2/Zen3 server) ---
    "amd epyc 7313": {"single": 1550, "multi": 10500},
    "amd epyc 7402": {"single": 1300, "multi": 11000},
    "amd epyc 7413": {"single": 1500, "multi": 13000},
    "amd epyc 7532": {"single": 1250, "multi": 13000},
    "amd epyc 7642": {"single": 1300, "multi": 16500},
    # --- AMD EPYC 7001 (Naples, first-gen Zen server) ---
    "amd epyc 7351p": {"single": 900, "multi": 5800},
    "amd epyc 7371": {"single": 1050, "multi": 7000},
    "amd epyc 7451": {"single": 880, "multi": 7800},
    # --- Intel Xeon E / E3 (Coffee Lake / Rocket Lake / Kaby Lake / Skylake) ---
    "intel xeon e-2274g": {"single": 1700, "multi": 5800},
    "intel xeon-e 2136": {"single": 1500, "multi": 6300},
    "intel xeon-e 2288g": {"single": 1750, "multi": 8300},
    "intel xeon-e 2386g": {"single": 1950, "multi": 8200},
    "intel xeon-e 2388g": {"single": 1950, "multi": 10000},
    "intel xeon-e3 1230v6": {"single": 1350, "multi": 4400},
    "intel xeon-e3 1245 v5": {"single": 1300, "multi": 4200},
    "intel xeon-e3 1270 v6": {"single": 1450, "multi": 4700},
    # --- Intel Xeon Silver / Gold (Cascade Lake / Ice Lake) ---
    "intel xeon silver 4214r": {"single": 1150, "multi": 7800},
    "intel xeon gold 6132": {"single": 1100, "multi": 9100},
    "intel xeon gold 6312u": {"single": 1350, "multi": 13500},
    # --- Intel Xeon E5 v2/v3/v4 (Ivy Bridge-EP / Haswell-EP / Broadwell-EP) ---
    # E5-1620v2/1650v2 are 2013 Ivy Bridge-EP; Geekbench 6 data is sparse, so
    # these two are best-effort estimates for relative ordering.
    "intel xeon e5-1620v2": {"single": 900, "multi": 3300},
    "intel xeon e5-1650v2": {"single": 880, "multi": 4500},
    "intel xeon e5-1650v4": {"single": 1100, "multi": 5200},
    "intel xeon e5-2680 v3": {"single": 920, "multi": 6800},
    # --- Intel Xeon-D (Broadwell-DE / Skylake-D SoC) ---
    "intel xeon-d 1520": {"single": 760, "multi": 2400},
    "intel xeon-d 1521": {"single": 800, "multi": 2500},
    "intel xeon-d 1540": {"single": 870, "multi": 4700},
    "intel xeon d-2123it": {"single": 930, "multi": 2600},
    "intel xeon-d 2141i": {"single": 960, "multi": 4100},
    # --- Intel Core (desktop) ---
    "intel core i7-7700k": {"single": 1600, "multi": 4900},
    "intel i7-6700k": {"single": 1500, "multi": 4700},
}


def _normalize(brand: str, model: str) -> str:
    """Fold OVH's ``brand``/``model`` into a stable lookup key.

    Lowercases and collapses internal whitespace so casing variants like
    ``"Epyc 7313"``/``"EPYC 4244P"`` and ``"Xeon D-2123IT"``/``"Xeon D-2123iT"``
    map to a single key.
    """
    combined = f"{brand or ''} {model or ''}"
    return " ".join(combined.split()).lower()


def lookup(brand: str, model: str) -> dict[str, Any] | None:
    """Return ``{"single", "multi"}`` for a CPU, or ``None`` if not in the table."""
    entry = GEEKBENCH6.get(_normalize(brand, model))
    return dict(entry) if entry is not None else None
