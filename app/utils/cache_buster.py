"""Content-hash based cache busting for static assets.

Computes a short SHA256 hash of file contents so the cache-busting
query string on <link>/<script> tags changes only when the underlying
file actually changes. Hashes are cached per (path, mtime, size) so
repeated calls are free, while an edited file still invalidates: the
cache used to be keyed on the path alone, which meant a long-running
server (``python run.py``, no --reload) kept serving the OLD ?v= hash
after a deploy, so browsers held on to their cached copy of the asset
until someone restarted the process.

Usage in Jinja2 template:
    <link href="/static/css/app.css?v={{ css_hash }}">
    <script src="/static/js/app.js?v={{ js_hash }}"></script>
"""
import hashlib
import os
from functools import lru_cache

BASE_PATH = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@lru_cache(maxsize=64)
def _hash_file(path: str, mtime_ns: int, size: int) -> str:
    """Hash a file's contents. ``mtime_ns``/``size`` are not read - they are
    part of the cache key so a changed file gets a fresh entry."""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


def get_file_hash(rel_path: str) -> str:
    """Return first 12 hex chars of SHA256 of file content.

    Returns "dev" if the file does not exist (e.g. CSS not yet built).
    The stat() on every call is what keeps the result honest after the
    file changes under a running server; the read+hash itself is cached.
    """
    path = rel_path if os.path.isabs(rel_path) else os.path.join(BASE_PATH, rel_path)
    try:
        st = os.stat(path)
    except OSError:
        return "dev"
    try:
        return _hash_file(path, st.st_mtime_ns, st.st_size)
    except OSError:
        return "dev"
