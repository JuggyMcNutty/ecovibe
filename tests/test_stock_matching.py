"""Catalog addon codes vs. availability addon codes, pinned.

OVH names the same hardware two different ways on the two endpoints these
matchers bridge: the catalog's `addonFamilies` and the availabilities feed
(`/dedicated/server/datacenter/availabilities`). Three known divergences:

  * a trailing product token on the catalog side
    (`ram-256g-ecc-2933-24rise08-ca` vs `ram-256g-ecc-2933`),
  * marketed vs. physical capacity (`512` vs `500`, `1920` vs `1900`,
    `3840` vs `3800`),
  * and two spellings of "no data disks": the catalog offers
    `softraid-0disk-24rise-*` where the feed reports `noraid-0`.

`addonShortCode` / `normalizeAddonCode` / `addonCodesMatch` in
`static/js/app.js` reconcile them, and a failure there is invisible in the
worst way -- the plan just quietly loses its OOS badge, its stock panel, and
its exact-combo FQN. Following `test_charts.py`, the equivalence tables are
read out of app.js and the matcher is mirrored here so the *data* is pinned in
CI (there is no JS runtime in this environment); the last test pins the source
line that applies the alias table, so removing the code path fails too.

Fixtures are real code pairs, taken from the live US catalog and availabilities
feed on 2026-08-07 (143 plans / 15,731 availability entries).
"""
import json
import os
import re

import pytest

APP_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "js", "app.js"
)


# ----- the tables, read from the source they live in -----


def _js_object(name):
    src = open(APP_JS).read()
    m = re.search(r"const " + name + r" = (\{.*?\});", src, re.S)
    assert m, f"{name} not found in app.js"
    # JS object literals with unquoted/single-quoted keys -> JSON.
    body = m.group(1).replace("'", '"')
    body = re.sub(r"(\{|,)\s*([A-Za-z0-9_-]+)\s*:", r'\1"\2":', body)
    return json.loads(body)


STORAGE_CAPACITY_MAP = _js_object("STORAGE_CAPACITY_MAP")
STORAGE_CODE_ALIASES = _js_object("STORAGE_CODE_ALIASES")


# ----- the matcher, mirrored from app.js -----


def _short(code):
    if not code:
        return ""
    segs = code.split("-")
    return "-".join(segs[:-2]) if len(segs) > 2 else code


def _normalize(code):
    if not code:
        return ""

    def cap(m):
        n = m.group(1)
        if n not in STORAGE_CAPACITY_MAP:
            return m.group(0)
        return str(STORAGE_CAPACITY_MAP[n]) + m.group(2)

    sized = re.sub(r"(\d+)(ssd|nvme|sata|sas|sa|hdd)", cap, code, flags=re.I)
    for frm, to in STORAGE_CODE_ALIASES.items():
        if sized == frm:
            return to
        if sized.startswith(frm + "-"):
            return to + sized[len(frm):]
    return sized


def _matches(a, b):
    if not a or not b:
        return True
    na, nb = _normalize(a), _normalize(b)
    if na == nb:
        return True
    shorter, longer = (na, nb) if len(na) < len(nb) else (nb, na)
    return longer.startswith(shorter + "-") or longer == shorter


# ----- the region suffix -----


@pytest.mark.parametrize("full,short", [
    ("ram-256g-ecc-2933-24rise08-ca", "ram-256g-ecc-2933"),
    ("softraid-0disk-24rise-ca", "softraid-0disk"),
    ("noraid-0-24rise07-v1-ca", "noraid-0-24rise07"),
    ("softraid-2x480ssd-sata-system-24rise-ca", "softraid-2x480ssd-sata-system"),
])
def test_short_code_strips_the_product_and_region_tokens(full, short):
    assert _short(full) == short


# ----- what must match -----


@pytest.mark.parametrize("catalog,stock", [
    # The bug: OVH's catalog calls a diskless config `softraid-0disk` on the
    # 24rise082/24rise092 plans while the feed calls it `noraid-0`. Both carry
    # the invoiceName "No storage drive", and the 24rise07-v1 plans use
    # `noraid-0` on BOTH sides -- so these are one product, spelled twice.
    ("softraid-0disk", "noraid-0"),
    ("noraid-0-24rise07", "noraid-0"),
    # A trailing product token the short-code heuristic didn't strip.
    ("softraid-2x480ssd-sata-system-24rise07", "softraid-2x480ssd-sata-system"),
    ("ram-16g", "ram-16g-ecc-2133"),
    # Marketed vs. physical capacity.
    ("softraid-2x512nvme", "softraid-2x500nvme"),
    ("softraid-2x1920nvme", "softraid-2x1900nvme"),
    ("softraid-2x3840nvme", "softraid-2x3800nvme"),
    # Identical on both sides.
    ("ram-256g-ecc-2933", "ram-256g-ecc-2933"),
])
def test_equivalent_codes_match(catalog, stock):
    assert _matches(catalog, stock)


# ----- what must NOT match -----


@pytest.mark.parametrize("catalog,stock", [
    # `noraid-0` is no disk at all; `noraid-1x120ssd` is a 120GB SSD. The alias
    # must not collapse the whole `noraid-` family onto one another.
    ("softraid-0disk", "noraid-1x120ssd"),
    ("noraid-0", "noraid-1x120ssd"),
    ("softraid-0disk", "softraid-2x1920nvme"),
    ("ram-256g-ecc-2933", "ram-512g-ecc-2933"),
    ("softraid-2x1920nvme", "softraid-4x1920nvme"),
])
def test_distinct_configs_do_not_match(catalog, stock):
    assert not _matches(catalog, stock)


# ----- the six plans the alias exists for -----


# The five storage codes the availabilities feed reports for every
# 24rise082/24rise092 plan, verified live on 2026-08-07.
RISE08_STOCK_STORAGE = [
    "noraid-0",
    "softraid-2x1920nvme",
    "softraid-2x3840nvme",
    "softraid-4x1920nvme",
    "softraid-4x3840nvme",
]


@pytest.mark.parametrize("default_mem,default_stor", [
    ("ram-256g-ecc-2933-24rise08-ca", "softraid-0disk-24rise-ca"),
    ("ram-256g-ecc-2933-24rise08-eu", "softraid-0disk-24rise-eu"),
    ("ram-256g-ecc-2933-24rise08-us", "softraid-0disk-24rise-us"),
    ("ram-256g-ecc-2933-24rise09-ca", "softraid-0disk-24rise-ca"),
    ("ram-256g-ecc-2933-24rise09-eu", "softraid-0disk-24rise-eu"),
    ("ram-256g-ecc-2933-24rise09-us", "softraid-0disk-24rise-us"),
])
def test_the_included_config_of_each_affected_plan_resolves_to_one_stock_entry(default_mem, default_stor):
    """These six were the whole failure set: 6 of the 143 live plans, all of
    them the same mismatch. Their included (free) config is 256GB RAM + no data
    drive, and it has to land on exactly ONE feed entry -- landing on none is
    the reported bug, landing on several would make buildFqn() pick a config
    the user did not choose."""
    mem = _short(default_mem)
    stor = _short(default_stor)
    hits = [s for s in RISE08_STOCK_STORAGE if _matches(stor, s)]
    assert hits == ["noraid-0"], hits
    assert _matches(mem, "ram-256g-ecc-2933")
    assert not _matches(mem, "ram-512g-ecc-2933")


# ----- the source path that applies the table -----


def test_normalize_actually_applies_the_alias_table():
    """The table is inert unless normalizeAddonCode walks it -- and it has to
    rewrite only the descriptor prefix, so the trailing product token survives
    for addonCodesMatch()'s prefix rule."""
    src = open(APP_JS).read()
    start = src.index("function normalizeAddonCode(")
    body = src[start:src.index("\nfunction ", start + 1)]
    assert "STORAGE_CODE_ALIASES" in body
    assert "startsWith(from + '-')" in body
    assert "slice(from.length)" in body
