"""Config parsing tests."""
import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://a.com,https://b.com", ["https://a.com", "https://b.com"]),
        ("https://a.com, https://b.com ", ["https://a.com", "https://b.com"]),
        ('["https://c.com"]', ["https://c.com"]),
        ("", []),
        ("https://only.com", ["https://only.com"]),
    ],
)
def test_cors_origins_accepts_comma_or_json(monkeypatch, value, expected):
    """OVH_CORS_ORIGINS must accept the README's comma-separated form as well
    as a JSON array (a bare comma-separated string is not valid JSON and used
    to crash startup)."""
    monkeypatch.setenv("OVH_CORS_ORIGINS", value)
    assert Settings().cors_origins == expected


def test_cors_origins_defaults_empty(monkeypatch):
    monkeypatch.delenv("OVH_CORS_ORIGINS", raising=False)
    assert Settings().cors_origins == []
