"""Tests for the runtime log viewer (LogBus + /api/logs)."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_logs_endpoint_shape(client):
    r = client.get("/api/logs")
    assert r.status_code == 200
    body = r.json()
    assert "logs" in body
    assert "sources" in body
    assert isinstance(body["logs"], list)
    assert isinstance(body["sources"], list)


def test_emitted_record_appears(client):
    logging.getLogger("app.services.monitor").info("hello ecovibe %s", 42)
    body = client.get("/api/logs").json()
    messages = [e["message"] for e in body["logs"]]
    assert "hello ecovibe 42" in messages
    assert "monitor" in body["sources"]
    # The entry carries the structured fields the frontend renders.
    entry = next(e for e in body["logs"] if e["message"] == "hello ecovibe 42")
    assert entry["level"] == "INFO"
    assert entry["source"] == "monitor"
    assert "ts" in entry


def test_level_filter_narrows_results(client):
    log = logging.getLogger("app.api.checkout")
    log.info("an info line")
    log.warning("a warning line")
    body = client.get("/api/logs", params={"level": "WARNING"}).json()
    messages = [e["message"] for e in body["logs"]]
    assert "a warning line" in messages
    assert "an info line" not in messages


def test_source_filter(client):
    logging.getLogger("app.services.notifier").info("notifier line")
    logging.getLogger("app.api.orders").info("orders line")
    body = client.get("/api/logs", params={"source": "notifier"}).json()
    messages = [e["message"] for e in body["logs"]]
    assert "notifier line" in messages
    assert "orders line" not in messages


def test_search_filter(client):
    logging.getLogger("app.services.monitor").info("needle in haystack")
    logging.getLogger("app.services.monitor").info("something else")
    body = client.get("/api/logs", params={"search": "needle"}).json()
    messages = [e["message"] for e in body["logs"]]
    assert messages == ["needle in haystack"]


def test_limit_out_of_bounds_rejected(client):
    assert client.get("/api/logs", params={"limit": 0}).status_code == 422
    assert client.get("/api/logs", params={"limit": 99999}).status_code == 422
