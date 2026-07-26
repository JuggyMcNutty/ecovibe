"""Cache-busting hashes must follow the file, not just its path."""

from app.utils.cache_buster import get_file_hash


def test_hash_is_stable_for_unchanged_file(tmp_path):
    f = tmp_path / "app.js"
    f.write_text("console.log(1)")
    assert get_file_hash(str(f)) == get_file_hash(str(f))


def test_hash_changes_when_the_file_changes(tmp_path):
    """Regression: the hash was cached on the path alone, so a long-running
    server (python run.py, no --reload) kept emitting the OLD ?v= after a
    deploy and browsers never picked up the new asset."""
    f = tmp_path / "app.js"
    f.write_text("console.log(1)")
    before = get_file_hash(str(f))

    f.write_text("console.log(2) // edited under a running server")
    assert get_file_hash(str(f)) != before


def test_missing_file_returns_dev(tmp_path):
    assert get_file_hash(str(tmp_path / "not-built.css")) == "dev"
