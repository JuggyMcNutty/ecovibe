"""Tests for the Geekbench 6 lookup table and its wiring into productSpecs."""

import logging

from app.api import catalog
from app.api.catalog import _build_product_specs
from app.services import geekbench


def test_lookup_is_case_insensitive():
    # OVH reports the same silicon with inconsistent casing ("Epyc" vs "EPYC");
    # both must resolve to the same entry.
    a = geekbench.lookup("AMD", "Epyc 7313")
    b = geekbench.lookup("amd", "EPYC 7313")
    assert a is not None
    assert a == b
    assert set(a) == {"single", "multi"}


def test_lookup_collapses_whitespace():
    assert geekbench.lookup("Intel", "Xeon-E  2388G") == geekbench.lookup(
        "Intel", "Xeon-E 2388G"
    )


def test_lookup_unknown_cpu_returns_none():
    # A CPU not in the catalog / not carried in our table.
    assert geekbench.lookup("Intel", "Xeon-D 9999") is None
    assert geekbench.lookup("AMD", "Totally Made Up 9000") is None


def test_lookup_covers_catalog_cpus_that_were_previously_missing():
    # These all appear in OVH's live US/CA ECO catalog and used to return None,
    # which broke sort-by-CPU-score (they fell to the bottom). Every one must
    # now resolve to a {single, multi} entry.
    cases = [
        ("AMD", "EPYC TURIN 9455"),
        ("AMD", "Epyc 7351p"),
        ("AMD", "Epyc 7371"),
        ("AMD", "Epyc 7451"),
        ("Intel", "Xeon Gold 6132"),
        ("Intel", "Xeon E5-2680 v3"),
        ("Intel", "Xeon D-2123IT"),
        ("Intel", "Xeon-D 2141I"),
        ("Intel", "Xeon-D 1520"),
        ("Intel", "Xeon-D 1521"),
        ("Intel", "Xeon-D 1540"),
        ("Intel", "Xeon E5-1620v2"),
        ("Intel", "Xeon E5-1650v2"),
        ("Intel", "Xeon E5-1650v4"),
    ]
    for brand, model in cases:
        entry = geekbench.lookup(brand, model)
        assert entry is not None, f"{brand} {model} missing from table"
        assert set(entry) == {"single", "multi"}
        assert entry["single"] > 0 and entry["multi"] > 0


def test_lookup_casing_variant_of_catalog_cpu():
    # OVH reports the same Skylake-D chip as both "Xeon D-2123IT" and
    # "Xeon D-2123iT"; both must fold to the same entry.
    assert geekbench.lookup("Intel", "Xeon D-2123IT") == geekbench.lookup(
        "Intel", "Xeon D-2123iT"
    )


def test_lookup_returns_a_copy():
    # Mutating the result must not corrupt the shared table.
    first = geekbench.lookup("AMD", "Ryzen 9 5900X")
    first["multi"] = 0
    assert geekbench.lookup("AMD", "Ryzen 9 5900X")["multi"] != 0


def _product(name, brand, model):
    return {
        "name": name,
        "description": f"{brand} {model}",
        "blobs": {"technical": {"server": {"cpu": {"brand": brand, "model": model}}}},
    }


def test_build_product_specs_attaches_geekbench6():
    catalog = {
        "products": [
            _product("known", "AMD", "Ryzen 9 5900X"),
            _product("unknown", "Intel", "Xeon-D 9999"),
        ]
    }
    specs = _build_product_specs(catalog)
    assert specs["known"]["cpu"]["geekbench6"] == {"single": 2250, "multi": 14200}
    # Unknown CPU present in the catalog but not in our table -> None (no badge).
    assert specs["unknown"]["cpu"]["geekbench6"] is None


def test_build_product_specs_warns_once_for_unknown_cpu(caplog):
    # An unknown CPU should surface a warning (so the gap is noticed and the
    # sort doesn't silently break); a known CPU should not.
    catalog._warned_missing_gb6.clear()
    known = {"products": [_product("k", "AMD", "Ryzen 9 5900X")]}
    unknown = {"products": [_product("u", "Intel", "Xeon-D 9999")]}
    with caplog.at_level(logging.WARNING, logger="app.api.catalog"):
        _build_product_specs(known)
        assert "No Geekbench 6 score" not in caplog.text
        _build_product_specs(unknown)
        assert "No Geekbench 6 score" in caplog.text
        # Second pass over the same unknown CPU is deduped (still one record).
        caplog.clear()
        _build_product_specs(unknown)
        assert "No Geekbench 6 score" not in caplog.text
