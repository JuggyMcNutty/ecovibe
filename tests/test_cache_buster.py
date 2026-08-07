"""Cache-busting hashes must follow the file, not just its path."""

from app.utils.cache_buster import get_file_hash, get_tree_hash


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


# ----- whole-bundle hashing (the vendored noVNC scripts) -----


def _bundle(root):
    (root / "core").mkdir()
    (root / "core" / "a.js").write_text("a")
    (root / "core" / "sub").mkdir()
    (root / "core" / "sub" / "b.js").write_text("b")
    (root / "core" / "notes.md").write_text("not a script")
    return root


def test_tree_hash_is_stable(tmp_path):
    _bundle(tmp_path)
    assert get_tree_hash(str(tmp_path)) == get_tree_hash(str(tmp_path))


def test_tree_hash_changes_when_any_file_in_it_changes(tmp_path):
    """The ATEN handshake fix lived in one of 16 vendored scripts, none of
    which had a cache buster -- so browsers kept the broken copy after deploy.
    A change anywhere in the bundle must move the hash."""
    _bundle(tmp_path)
    before = get_tree_hash(str(tmp_path))

    (tmp_path / "core" / "sub" / "b.js").write_text("b // patched")
    assert get_tree_hash(str(tmp_path)) != before


def test_tree_hash_ignores_non_matching_files(tmp_path):
    """Editing PROVENANCE.md shouldn't force every client to re-download."""
    _bundle(tmp_path)
    before = get_tree_hash(str(tmp_path))

    (tmp_path / "core" / "notes.md").write_text("rewritten")
    assert get_tree_hash(str(tmp_path)) == before


def test_tree_hash_covers_renames(tmp_path):
    """Hashing contents alone would collide when a file is renamed."""
    _bundle(tmp_path)
    before = get_tree_hash(str(tmp_path))

    (tmp_path / "core" / "a.js").rename(tmp_path / "core" / "z.js")
    assert get_tree_hash(str(tmp_path)) != before


def test_tree_hash_of_missing_directory_is_dev(tmp_path):
    assert get_tree_hash(str(tmp_path / "nope")) == "dev"
