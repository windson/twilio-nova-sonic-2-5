"""REST API tests using the FastAPI TestClient.

The Twilio client is mocked (via ``app.main.place_outbound_call``) and the
store is seeded directly, so no real Twilio / AWS / network call is made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.config import Settings
from app.main import app
from app.store import InMemoryStore, set_default_store
from app.twilio_client import OutboundCallResult

client = TestClient(app)


def _settings(**overrides) -> Settings:
    base = dict(
        nova_model_id="amazon.nova-2-5-sonic",
        aws_region="us-east-1",
        app_log_group="/ecs/ar-collections-nova-sonic/app",
        extraction_model_id="amazon.nova-lite-v1:0",
        code_interpreter_id="aws.codeinterpreter.v1",
        payment_link_base_url="https://pay.example.com/",
        twilio_trial_mode=True,
        twilio_account_sid="AC_test_sid",
        twilio_auth_token="test_token_value",
        twilio_from_number="+15005550006",
        twilio_verified_caller_id="+15005550010",
        public_base_url="https://example.ngrok.app",
        otel_enabled=True,
        otel_exporter="console",
        otel_endpoint=None,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def store() -> InMemoryStore:
    """Fresh in-memory store installed as the process default for each test."""
    s = InMemoryStore()
    set_default_store(s)
    return s


def test_get_dues_returns_seeded_records(store):
    resp = client.get("/api/dues")
    assert resp.status_code == 200
    dues = resp.json()["dues"]
    ids = {d["account_id"] for d in dues}
    assert "ACC-1001" in ids
    assert "ACC-1002" in ids
    jordan = next(d for d in dues if d["account_id"] == "ACC-1001")
    assert jordan["debtor_name"] == "Jordan Rivera"
    assert jordan["amount_due"] == 428.57


def test_post_calls_places_call_with_mocked_twilio(store):
    fake_result = OutboundCallResult(
        call_sid="CA_fake_sid",
        to="+15005550010",
        from_number="+15005550006",
        account_id="ACC-1001",
    )
    with patch("app.main.get_settings", return_value=_settings()), patch(
        "app.main.place_outbound_call", return_value=fake_result
    ) as mock_place:
        resp = client.post("/api/calls", json={"account_id": "ACC-1001"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["call_sid"] == "CA_fake_sid"
    assert body["session_id"] == "CA_fake_sid"
    assert body["account_id"] == "ACC-1001"

    mock_place.assert_called_once()
    args, kwargs = mock_place.call_args
    # Callee is the verified caller id; the due record is passed through.
    assert args[0] == "+15005550010"
    assert args[1]["account_id"] == "ACC-1001"


def test_post_calls_unknown_account_returns_404(store):
    with patch("app.main.get_settings", return_value=_settings()):
        resp = client.post("/api/calls", json={"account_id": "NOPE"})
    assert resp.status_code == 404


def test_post_calls_missing_twilio_env_returns_clear_error_not_500(store):
    # Missing creds + PUBLIC_BASE_URL -> actionable 503, never an unhandled 500.
    bad = _settings(
        twilio_account_sid=None,
        twilio_auth_token=None,
        public_base_url=None,
    )
    with patch("app.main.get_settings", return_value=bad), patch(
        "app.main.place_outbound_call"
    ) as mock_place:
        resp = client.post("/api/calls", json={"account_id": "ACC-1001"})

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["error"] == "twilio_not_configured"
    assert "PUBLIC_BASE_URL" in detail["message"]
    # We short-circuit before ever attempting the call.
    mock_place.assert_not_called()


def test_get_calls_and_get_call_by_id_return_persisted_analytics(store):
    analytics = {
        "call_sid": "CA_persisted",
        "account_id": "ACC-1001",
        "total_e2e_ms": 12345.0,
        "nova_total_ms": 8000.0,
        "twilio_latency_ms": 42.0,
        "ttft_ms": 350.0,
        "turn_count": 3,
        "barge_in_count": 1,
        "turns": [
            {"index": 0, "duration_ms": 900.0, "turn_latency_ms": 120.0, "ttft_ms": 350.0},
        ],
        "transcripts": [{"role": "assistant", "text": "Hello", "is_final": True}],
        "events_log": [{"event_type": "call_placed"}],
    }
    store.save_call_analytics(analytics)

    list_resp = client.get("/api/calls")
    assert list_resp.status_code == 200
    calls = list_resp.json()["calls"]
    assert any(c["call_sid"] == "CA_persisted" for c in calls)
    summary = next(c for c in calls if c["call_sid"] == "CA_persisted")
    assert summary["turn_count"] == 3
    assert summary["total_e2e_ms"] == 12345.0

    detail_resp = client.get("/api/calls/CA_persisted")
    assert detail_resp.status_code == 200
    full = detail_resp.json()
    assert full["turns"][0]["duration_ms"] == 900.0
    assert full["transcripts"][0]["text"] == "Hello"
    assert full["events_log"][0]["event_type"] == "call_placed"


def test_calls_expose_postcall_intelligence_fields(store):
    analytics = {
        "call_sid": "CA_intel",
        "account_id": "ACC-1001",
        "total_e2e_ms": 5000.0,
        "turn_count": 2,
        "disposition": "promise_to_pay",
        "intelligence_status": "ready",
        "structured_data": {
            "payment_specifics": {
                "promised_amount": 428.57,
                "agreed_date": "2024-06-03",
                "preferred_payment_method": "card",
            },
            "contextual_data": {
                "reason_for_delinquency": "medical emergency",
                "customer_sentiment": "cooperative",
            },
        },
        "next_best_action": "Schedule follow-up call",
        "collection_paused": True,
        "escalated": False,
        "ptp_sms_status": "sent",
    }
    store.save_call_analytics(analytics)

    # List summary carries disposition + intelligence_status.
    calls = client.get("/api/calls").json()["calls"]
    summary = next(c for c in calls if c["call_sid"] == "CA_intel")
    assert summary["disposition"] == "promise_to_pay"
    assert summary["intelligence_status"] == "ready"

    # Detail carries the full structured data + NBA + trigger context.
    full = client.get("/api/calls/CA_intel").json()
    assert full["disposition"] == "promise_to_pay"
    assert full["next_best_action"] == "Schedule follow-up call"
    assert full["structured_data"]["payment_specifics"]["agreed_date"] == "2024-06-03"
    assert full["collection_paused"] is True
    assert full["ptp_sms_status"] == "sent"


def test_get_call_unknown_id_returns_404(store):
    resp = client.get("/api/calls/does-not-exist")
    assert resp.status_code == 404


async def test_launch_postcall_registers_offpath_task_and_discards_on_done(store):
    """LAUNCH-POINT guard: ``_launch_postcall`` (the seam called from the /media
    websocket teardown) must create an asyncio task, register it in the
    module-level ``_POSTCALL_TASKS`` set so it is not garbage-collected
    mid-flight, run OFF the current call path, and discard itself from the set
    via the done-callback once finished. Also exercises the done-callback's
    result logging. This fails if the launch is removed or the task is left only
    in a local variable (the dropped-task bug).
    """
    import asyncio

    from app import main as main_mod

    ran: list[str] = []

    async def fake_extract(record, store_arg, settings_arg):
        # Prove the launch is off-path: the task has not run yet at creation.
        ran.append(record["call_sid"])
        return {"intelligence_status": "ready"}

    record = {"call_sid": "CA_launch", "account_id": "ACC-1001"}
    store.save_call_analytics(record)

    with patch("app.main.extract_and_apply", fake_extract):
        task = main_mod._launch_postcall(record, store, _settings())
        # Registered + not yet executed (off the caller's path).
        assert task is not None
        assert task in main_mod._POSTCALL_TASKS
        assert ran == []
        # Let the fire-and-forget task run to completion.
        await task

    # It ran, and the done-callback discarded the strong reference.
    assert ran == ["CA_launch"]
    assert task not in main_mod._POSTCALL_TASKS


async def test_launch_postcall_done_callback_logs_and_discards_on_error(store):
    """The done-callback discards the task and logs even when the task raises
    (defensive: ``extract_and_apply`` normally never raises, but a scheduling
    bug must not leave a dangling reference or an unretrieved exception).
    """
    import asyncio

    from app import main as main_mod

    async def boom(record, store_arg, settings_arg):
        raise RuntimeError("unexpected")

    record = {"call_sid": "CA_boom", "account_id": "ACC-1001"}

    with patch("app.main.extract_and_apply", boom):
        task = main_mod._launch_postcall(record, store, _settings())
        assert task is not None
        with pytest.raises(RuntimeError):
            await task
        # Allow the done-callback to fire.
        await asyncio.sleep(0)

    assert task not in main_mod._POSTCALL_TASKS


def test_index_serves_html():
    # The dashboard is now a compiled React/Vite/shadcn bundle emitted into
    # web/ at image-build time. GET / serves web/index.html when the bundle has
    # been built, otherwise the placeholder landing page. Either way it must be
    # an HTML 200 that names the service and (when built) loads the module
    # bundle from the /static mount with a <div id="root"> mount point.
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "AR Collections Nova Sonic 2.5 Agent" in body
    web_index = main_mod._WEB_DIR / "index.html"
    if web_index.is_file():
        # Built bundle: React mount point + hashed JS served under /static/.
        assert 'id="root"' in body
        assert "/static/assets/" in body
    else:
        # Placeholder fallback (no frontend build present in this checkout).
        assert "Service is running" in body


def test_static_mount_serves_built_assets():
    # The /static mount serves whatever the frontend build emitted into web/.
    # When a bundle is present its hashed JS asset must be reachable under
    # /static/assets/; otherwise this checkout has no built frontend and we skip.
    assets_dir = main_mod._WEB_DIR / "assets"
    if not assets_dir.is_dir():
        pytest.skip("frontend bundle not built in this checkout")
    js_files = list(assets_dir.glob("*.js"))
    assert js_files, "expected a built JS asset in web/assets"
    resp = client.get(f"/static/assets/{js_files[0].name}")
    assert resp.status_code == 200
