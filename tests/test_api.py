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
    # The list summary exposes the created_at key (None when the
    # persisted record carried no wall-clock stamp) so the UI can join calls
    # under their due record and render WHEN each call happened.
    assert "created_at" in summary

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


def test_calls_serialize_analytics_fields(store):
    """The full record serialized by GET /api/calls/{id} must carry
    the fields consumed by the UI: twilio_insights,
    transport_delay_estimate_ms, per-turn tool_calls + response_wait_ms +
    turn_elapsed_ms + interrupted, barge_in_events, and async_tool_calls. The
    list summary also exposes transport_delay_estimate_ms.
    """
    analytics = {
        "call_sid": "CA_analytics",
        "account_id": "ACC-1001",
        "total_e2e_ms": 5000.0,
        "nova_total_ms": 3000.0,
        "transport_delay_estimate_ms": 42.0,
        "twilio_latency_ms": 42.0,
        "non_nova_ms": 2000.0,
        "turn_count": 2,
        "conversation_turns": [
            {
                "index": 0,
                "speaker": "assistant",
                "text": "Hello.",
                "response_wait_ms": 120.0,
                "latency_ms": 120.0,
                "turn_elapsed_ms": 500.0,
                "total_ms": 500.0,
                "interrupted": True,
                "barge_in_ms": [1500.0],
                "tool_calls": [
                    {"tool": "update_due_record", "kind": "sync", "t_ms": 1200.0}
                ],
            },
            {"index": 1, "speaker": "user", "text": "Hi.", "interrupted": False,
             "tool_calls": []},
        ],
        "barge_in_events": [{"type": "barge_in", "t_ms": 1500.0}],
        "async_tool_calls": [
            {"tool": "resolve_date_phrase", "kind": "async", "t_ms": None, "off_path": True}
        ],
        "twilio_insights": {
            "available": True,
            "source": "twilio_call_resource",
            "advanced_qos_available": False,
            "advanced_qos_reason": "advanced QoS add-on not enabled",
            "fields": {"status": "completed", "duration": "90", "from": "+15005550006"},
        },
    }
    store.save_call_analytics(analytics)

    # List summary carries the accurate-named transport estimate.
    calls = client.get("/api/calls").json()["calls"]
    summary = next(c for c in calls if c["call_sid"] == "CA_analytics")
    assert summary["transport_delay_estimate_ms"] == 42.0

    full = client.get("/api/calls/CA_analytics").json()
    # twilio_insights.
    assert full["twilio_insights"]["available"] is True
    assert full["twilio_insights"]["advanced_qos_available"] is False
    assert full["twilio_insights"]["fields"]["status"] == "completed"
    assert full["transport_delay_estimate_ms"] == 42.0
    # Accurate per-turn field names.
    asst = full["conversation_turns"][0]
    assert asst["response_wait_ms"] == 120.0
    assert asst["turn_elapsed_ms"] == 500.0
    # Per-turn tool_calls + call-level async_tool_calls.
    assert asst["tool_calls"][0]["tool"] == "update_due_record"
    assert asst["tool_calls"][0]["kind"] == "sync"
    assert full["async_tool_calls"][0]["kind"] == "async"
    assert full["async_tool_calls"][0]["t_ms"] is None
    # Interrupted + inline barge-in markers.
    assert asst["interrupted"] is True
    assert full["barge_in_events"] == [{"type": "barge_in", "t_ms": 1500.0}]


def test_call_record_carries_created_at_for_calls_under_dues(store):
    """CallMetrics stamps an absolute wall-clock created_at at session
    start, it is serialized on the full record, and the /api/calls list summary
    exposes it so the UI can join calls under their due record and show WHEN
    each call happened. Fails if created_at is dropped from either the record
    or the summary.
    """
    from app.metrics import CallMetrics

    m = CallMetrics(call_sid="CA_when", account_id="ACC-1001")
    m.on_session_start()
    m.on_session_end()
    record = m.to_call_analytics()
    assert isinstance(record.get("created_at"), str) and record["created_at"]
    store.save_call_analytics(record)

    calls = client.get("/api/calls").json()["calls"]
    summary = next(c for c in calls if c["call_sid"] == "CA_when")
    assert summary["created_at"] == record["created_at"]

    full = client.get("/api/calls/CA_when").json()
    assert full["created_at"] == record["created_at"]


def test_get_call_unknown_id_returns_404(store):
    resp = client.get("/api/calls/does-not-exist")
    assert resp.status_code == 404


def test_call_dashboard_url_scoped_to_call_sid_and_config(store):
    """List and detail records expose the configured per-call dashboard URL.

    The named-dashboard fragment contains only its valid name. Configuration
    supplies the region and log-group stack, without an AWS request.
    """
    analytics = {
        "call_sid": "CA_dash",
        "account_id": "ACC-1001",
        "total_e2e_ms": 5000.0,
        "turn_count": 2,
    }
    store.save_call_analytics(analytics)

    custom = _settings(
        aws_region="eu-west-1",
        app_log_group="/ecs/my-stack/app",
    )
    with patch("app.main.get_settings", return_value=custom):
        summary = next(
            c
            for c in client.get("/api/calls").json()["calls"]
            if c["call_sid"] == "CA_dash"
        )
        full = client.get("/api/calls/CA_dash").json()

    for url in (summary["dashboard_url"], full["dashboard_url"]):
        assert url
        # Built from the configured region (host + region query param).
        assert "eu-west-1.console.aws.amazon.com" in url
        assert "region=eu-west-1" in url
        # Opens the CONSOLIDATED per-call dashboard, not a bare logs stream.
        assert "#dashboards:name=" in url
        assert "logsV2:logs-insights" not in url
        # Dashboard name derived from the configured log group's stack segment.
        assert "my-stack-per-call" in url
        assert "ar-collections-nova-sonic" not in url
        # Name segment is EXACTLY the dashboard name, nothing fused on.
        name_segment = url.split("#dashboards:name=", 1)[1]
        assert name_segment == "my-stack-per-call"
        assert "$3F" not in url and "$3D" not in url and "params" not in url


def test_call_dashboard_url_builder_unit():
    """Unit-level assertions on the pure string builder: the query filters by
    call_sid against the given log group and surfaces the metric fields.
    """
    from app.dashboard import call_dashboard_query, call_dashboard_url

    q = call_dashboard_query("CA_unit", "/ecs/s/app")
    assert "SOURCE '/ecs/s/app'" in q
    assert "filter call_sid = 'CA_unit'" in q
    for field in ("ttft_ms", "turn_latency_ms", "duration_ms", "total_tokens"):
        assert field in q

    url = call_dashboard_url("CA_unit", aws_region="us-west-2", app_log_group="/ecs/s/app")
    assert url.startswith("https://us-west-2.console.aws.amazon.com/cloudwatch/home")
    # Clean named-dashboard fragment; name segment is exactly the name.
    assert url.endswith("#dashboards:name=s-per-call")
    assert "$3F" not in url and "$3D" not in url and "params" not in url
    assert "logsV2:logs-insights" not in url


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

    record = {"call_sid": "CA_launch", "account_id": "ACC-1001",
              "termination": {"telephone_completion_confirmed": True}}
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

    record = {"call_sid": "CA_boom", "account_id": "ACC-1001",
              "termination": {"telephone_completion_confirmed": True}}
    store.save_call_analytics(record)

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
    # An HTML 200 that names the service and (when built) loads the module
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
