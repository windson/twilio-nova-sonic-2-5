"""Health endpoint smoke tests."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz_ok():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_index_serves_html():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "AR Collections Nova Sonic 2.5 Agent" in resp.text


def test_config_summary_has_no_secret_values():
    resp = client.get("/api/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["nova_model_id"] == "amazon.nova-2-5-sonic"
    assert body["aws_region"]
    # Only booleans / safe strings are exposed, never raw secrets.
    assert isinstance(body["twilio_configured"], bool)
