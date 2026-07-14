"""Content-hash based cache busting for static assets.

Computes a short SHA256 hash of file contents so the cache-busting
query string on <link>/<script> tags changes only when the underlying
file actually changes. Results are cached with lru_cache so repeated
calls during a process lifetime are free.

Usage in Jinja2 template:
    <link href="/static/css/app.css?v={{ css_hash }}">
    <script src="/static/js/app.js?v={{ js_hash }}"></script>
"""
import hashlib
import os
from functools import lru_cache

BASE_PATH = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@lru_cache(maxsize=64)
def get_file_hash(rel_path: str) -> str:
    """Return first 12 hex chars of SHA256 of file content.

    Returns "dev" if the file does not exist (e.g. CSS not yet built).
    """
    path = rel_path if os.path.isabs(rel_path) else os.path.join(BASE_PATH, rel_path)
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except FileNotFoundError:
        return "dev"
