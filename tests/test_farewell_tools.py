"""Public tool contracts for the controller-owned Nova farewell handshake."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from types import SimpleNamespace

import pytest

from app.call_lifecycle import CallLifecycleController, CallState
from app.collections_agent import CallToolRuntime, build_tools, get_system_prompt
from app.metrics import CallMetrics
from app.store import InMemoryStore


def _context(identity: str):
    return SimpleNamespace(tool_use={"toolUseId": identity}, cancel_signal=threading.Event())


def _tools(runtime):
    return {tool.tool_name: tool for tool in build_tools(InMemoryStore(), runtime=runtime)}


def test_prompt_requires_ready_nonce_handshake_before_configured_farewell():
    prompt = get_system_prompt(InMemoryStore().get_due("ACC-1001"), closing_greeting="Thank you. Goodbye.")
    ending = prompt.split("[ENDING]", 1)[1]
    assert ending.index("`end_conversation`") < ending.index("`begin_farewell`")
    assert "Only after `begin_farewell` confirms readiness" in ending
    assert "say exactly the greeting returned by that tool" in ending
    assert "Do not translate or paraphrase it" in ending
    assert "Thank you. Goodbye." not in prompt
    assert "language='en', 'fr', or 'es'" in ending
    assert "Never speak an `attempt_id`, nonce" in ending
    assert "do not give or automatically repeat the farewell" in ending


@pytest.mark.asyncio
@pytest.mark.parametrize("language,greeting", [
    ("en", "A distinct custom closing. Goodbye."),
    ("fr", "Merci pour votre temps. Au revoir."),
    ("es", "Gracias por su tiempo. Adiós."),
])
async def test_farewell_words_are_available_only_after_readiness(language, greeting):
    from tests.test_call_lifecycle import Harness

    h = Harness()
    h.controller.closing_greeting = "A distinct custom closing. Goodbye."
    try:
        h.register(("end", "end_conversation"))
        result = await h.controller.request_end(tool_use_id="end", language=language)
        assert result["ready"]
        assert greeting not in str(result)
        assert "say exactly" not in result["instruction"]
        duplicate = await h.controller.request_end(tool_use_id="end", language=language)
        assert greeting not in str(duplicate)
        invalid = await h.controller.begin_farewell("stale", tool_use_id="begin")
        assert invalid["status"] == "error" and greeting not in str(invalid)
        h.results("end")
        h.register(("begin", "begin_farewell"))
        grant = await h.controller.begin_farewell(result["attempt_id"], tool_use_id="begin")
        assert grant["ready"] and greeting in grant["instruction"]
    finally:
        await h.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,language,greeting", [
    ({}, "en", "Thank you for your time. Goodbye."),
    ({"language": "fr"}, "fr", "Merci pour votre temps. Au revoir."),
    ({"language": "es"}, "es", "Gracias por su tiempo. Adiós."),
])
async def test_terminal_tools_pass_context_ids_to_late_bound_controller(caplog, arguments, language, greeting):
    events, calls = [], []
    runtime = CallToolRuntime(event_sink=events.append)
    tools = _tools(runtime)

    class Lifecycle:
        async def request_end(self, reason=None, tool_use_id=None, language="en"):
            calls.append(("request", reason, tool_use_id, language))
            return {"status": "ready", "attempt_id": "PRIVATE_ATTEMPT_NONCE", "language": language}

        def begin_farewell(self, attempt_id, tool_use_id=None):
            calls.append(("begin", attempt_id, tool_use_id))
            return {"status": "ready", "ready": True, "language": language,
                    "instruction": f"Now say exactly: {greeting} Then listen while playback finishes."}

    runtime.lifecycle = Lifecycle()
    assert inspect.iscoroutinefunction(tools["end_conversation"]._tool_func)
    assert inspect.iscoroutinefunction(tools["begin_farewell"]._tool_func)
    with caplog.at_level(logging.INFO):
        request = await tools["end_conversation"]._tool_func(
            reason="caller finished", tool_context=_context("request-1"), **arguments,
        )
        grant = await tools["begin_farewell"]._tool_func(
            attempt_id=request["attempt_id"], tool_context=_context("grant-1"),
        )
    assert grant["ready"] is True
    assert request["language"] == grant["language"] == language
    assert grant["instruction"] == f"Now say exactly: {greeting} Then listen while playback finishes."
    assert calls == [("request", "caller finished", "request-1", language), ("begin", "PRIVATE_ATTEMPT_NONCE", "grant-1")]
    finished = [event for event in events if event["event"] == "complete"]
    assert [event["tool_use_id"] for event in finished] == ["request-1", "grant-1"]
    assert all(event["completion_semantics"] == "ready" and event["ready"] for event in finished)
    assert "PRIVATE_ATTEMPT_NONCE" not in str(events) + caplog.text


@pytest.mark.asyncio
async def test_pending_duplicate_does_not_wait_for_request_or_close_business_writes():
    entered, release = asyncio.Event(), asyncio.Event()
    events = []

    class Lifecycle:
        tool_admission_open = True
        calls = 0

        async def request_end(self, reason=None, tool_use_id=None, language="en"):
            assert language == "en"
            self.calls += 1
            if self.calls > 1:
                return {"status": "pending", "ready": False}
            entered.set()
            await release.wait()
            return {"status": "ready", "attempt_id": "fresh-attempt"}

    lifecycle = Lifecycle()
    runtime = CallToolRuntime(lifecycle=lifecycle, event_sink=events.append)
    tools = _tools(runtime)
    first = asyncio.create_task(tools["end_conversation"]())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        duplicate = await asyncio.wait_for(tools["end_conversation"](), 0.5)
        assert duplicate == {"status": "pending", "ready": False}
        assert not first.done()
        # The controller handles invalidating the attempt; the wrapper must
        # not preemptively reject a business tool during settlement.
        saved = await tools["update_due_record"](account_id="ACC-1001", new_status="callback")
        assert isinstance(saved, str)
        assert runtime.failed_write_count == 0
        pending = [event for event in events if event.get("completion_semantics") == "pending"]
        assert len(pending) == 1 and pending[0]["ready"] is False
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await runtime.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [("end_conversation", {}), ("begin_farewell", {"attempt_id": "unknown"})])
async def test_terminal_tools_without_controller_report_unavailable(name, arguments):
    events = []
    runtime = CallToolRuntime(event_sink=events.append)
    result = await _tools(runtime)[name](**arguments)
    assert result["status"] == "error" and "unavailable" in result["content"][0]["text"]
    assert events[-1]["completion_semantics"] == "error"
    assert events[-1]["ready"] is False
    assert runtime.failed_write_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [("end_conversation", {}), ("begin_farewell", {"attempt_id": "unknown"})])
async def test_controller_errors_stay_native_sdk_errors(name, arguments):
    class Lifecycle:
        async def request_end(self, **kwargs):
            return {"status": "error", "reason": "settlement_timeout"}

        def begin_farewell(self, *args, **kwargs):
            return {"status": "error", "reason": "invalid_attempt"}

    events = []
    tool = _tools(CallToolRuntime(lifecycle=Lifecycle(), event_sink=events.append))[name]
    use = {"toolUseId": "native-error", "name": name, "input": arguments}
    streamed = [event async for event in tool.stream(use, {}, _tool_context=_context("native-error"))]
    assert streamed[-1].tool_result["status"] == "error"
    assert streamed[-1].tool_result["toolUseId"] == "native-error"
    assert events[-1]["ready"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [("end_conversation", {}), ("begin_farewell", {"attempt_id": "unknown"})])
async def test_terminal_controller_exception_is_redacted(name, arguments, caplog):
    class Lifecycle:
        async def request_end(self, **kwargs):
            raise RuntimeError("SYNTHETIC_LIFECYCLE_SECRET")

        def begin_farewell(self, *args, **kwargs):
            raise RuntimeError("SYNTHETIC_LIFECYCLE_SECRET")

    events = []
    runtime = CallToolRuntime(lifecycle=Lifecycle(), event_sink=events.append)
    result = await _tools(runtime)[name](**arguments)
    assert result["status"] == "error"
    assert events[-1]["error_type"] == "RuntimeError" and events[-1]["ready"] is False
    assert "SYNTHETIC_LIFECYCLE_SECRET" not in str(result) + str(events) + caplog.text
    assert runtime.failed_write_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [("end_conversation", {}), ("begin_farewell", {"attempt_id": "nonce"})])
async def test_cancelled_terminal_request_propagates_and_later_request_reaches_controller(name, arguments):
    entered = asyncio.Event()

    class Lifecycle:
        tool_admission_open = True
        calls = 0
        cancelled = False

        async def request_end(self, **kwargs):
            self.calls += 1
            if self.calls > 1:
                return {"status": "ready", "attempt_id": "retry"}
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def begin_farewell(self, attempt_id, **kwargs):
            return await self.request_end(**kwargs)

    lifecycle = Lifecycle()
    events = []
    runtime = CallToolRuntime(lifecycle=lifecycle, event_sink=events.append)
    terminal = _tools(runtime)[name]
    waiting = asyncio.create_task(terminal(**arguments))
    await asyncio.wait_for(entered.wait(), 1)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert lifecycle.cancelled and events[-1]["status"] == "cancelled"
    assert events[-1]["ready"] is False
    assert (await terminal(**arguments))["attempt_id"] == "retry"
    assert runtime.failed_write_count == 0


@pytest.mark.asyncio
async def test_cancelled_old_end_waiter_cannot_abort_new_attempt(monkeypatch):
    events = []
    runtime = CallToolRuntime(event_sink=events.append)

    async def no_io(*args):
        pytest.fail("an ending request must not perform telephone teardown")

    lifecycle = CallLifecycleController(
        call_sid="CA_attempt_ownership", stream_sid="MZ_attempt_ownership", runtime=runtime,
        metrics=CallMetrics(call_sid="CA_attempt_ownership"),
        hangup=no_io, persist=no_io, close_websocket=no_io,
    )
    lifecycle.observe_event({"type": "bidi_connection_start"})
    lifecycle.register_tool_group([
        {"toolUseId": "business-pending", "name": "update_due_record"},
        {"toolUseId": "end-old", "name": "end_conversation"},
    ])
    entered = [asyncio.Event(), asyncio.Event()]
    wait_count = 0

    async def queued_wait(deadline):
        nonlocal wait_count
        index = wait_count
        wait_count += 1
        entered[index].set()
        # Keep the old waiter queued until explicitly cancelled, even though
        # caller activity has already invalidated its attempt. This exposes
        # cleanup ownership without depending on event-loop scheduling luck.
        await asyncio.Future()

    monkeypatch.setattr(lifecycle, "_wait_change", queued_wait)
    end = _tools(runtime)["end_conversation"]
    old = asyncio.create_task(end._tool_func(tool_context=_context("end-old")))
    new = None
    try:
        await asyncio.wait_for(entered[0].wait(), 1)
        original = lifecycle.attempt
        lifecycle.observe_event({"type": "bidi_transcript_start", "role": "user", "content_id": "caller-resumed"})
        lifecycle.register_tool_group([{"toolUseId": "end-new", "name": "end_conversation"}])
        new = asyncio.create_task(end._tool_func(tool_context=_context("end-new")))
        await asyncio.wait_for(entered[1].wait(), 1)
        current = lifecycle.attempt
        assert current is not None and current is not original and current.end_id == "end-new"
        old.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old
        assert lifecycle.attempt is current
        assert lifecycle.state == CallState.SETTLING_TOOLS
        assert current.invalid_reason is None and lifecycle.attempt_count == 2
        old_outcome = [event for event in events if event["tool_use_id"] == "end-old"][-1]
        assert old_outcome["status"] == "cancelled" and old_outcome["ready"] is False
    finally:
        old.cancel()
        if new is not None:
            new.cancel()
        await asyncio.gather(old, *([new] if new is not None else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error_result", "exception"])
async def test_cancelled_write_awaiter_preserves_worker_counts_and_completion_notification(outcome):
    entered, release = threading.Event(), threading.Event()

    class Lifecycle:
        tool_admission_open = True
        notifications = 0

        def writes_changed(self):
            self.notifications += 1

    lifecycle = Lifecycle()
    events = []
    runtime = CallToolRuntime(lifecycle=lifecycle, event_sink=events.append)

    def operation():
        entered.set()
        assert release.wait(2)
        if outcome == "exception":
            raise RuntimeError("SYNTHETIC_WRITE_SECRET")
        if outcome == "error_result":
            return {"status": "error", "content": [{"text": "write not confirmed"}]}
        return "saved"

    waiting = asyncio.create_task(runtime.execute("write", operation))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert runtime.pending_write_count == 1 and runtime.failed_write_count == 0
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert runtime.pending_write_count == 1 and runtime.failed_write_count == 0
        release.set()
        await asyncio.wait_for(runtime.drain(), 1)
        assert runtime.pending_write_count == 0
        assert runtime.failed_write_count == int(outcome != "success")
        assert lifecycle.notifications == 1
        assert events[-1]["status"] == ("success" if outcome == "success" else "error")
    finally:
        release.set()
        await runtime.drain()


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["controller", "runtime", "cancel_signal"])
async def test_rejected_write_does_not_count_as_failed_worker(rejection):
    lifecycle = SimpleNamespace(tool_admission_open=rejection != "controller")
    runtime = CallToolRuntime(lifecycle=lifecycle)
    context = _context("rejected-write")
    if rejection == "runtime":
        runtime.close_admission()
    elif rejection == "cancel_signal":
        context.cancel_signal.set()
    result = await runtime.execute("write", lambda: pytest.fail("write must not start"), context)
    assert result["status"] == "error"
    assert runtime.pending_write_count == runtime.failed_write_count == 0


@pytest.mark.asyncio
async def test_failed_success_callback_does_not_reclassify_committed_write(caplog):
    runtime = CallToolRuntime()

    def callback():
        raise RuntimeError("SYNTHETIC_CALLBACK_SECRET")

    assert await runtime.execute("write", lambda: "saved", on_success=callback) == "saved"
    assert runtime.failed_write_count == 0
    assert "SYNTHETIC_CALLBACK_SECRET" not in caplog.text
