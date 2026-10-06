"""Smoke tests: the app boots and the system endpoints answer."""

from fastapi.testclient import TestClient

from app.main import create_app

client = TestClient(create_app())


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_ping():
    r = client.get("/api/v1/system/ping")
    assert r.status_code == 200
    assert r.json() == {"ping": "pong"}


def test_capabilities_lists_three_modes():
    r = client.get("/api/v1/system/capabilities")
    assert r.status_code == 200
    assert len(r.json()["modes"]) == 3
