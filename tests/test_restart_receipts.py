"""Superseded provider receipts cannot poison a later closing handshake.

These use the deterministic controller harness and synthetic writes only.
The SDK can drop a previously observed event before its output callback.
"""

import asyncio
import base64
import threading
from types import SimpleNamespace

import pytest
from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiConnectionRestartEvent,
    BidiConnectionStartEvent,
    BidiResponseStartEvent,
    BidiToolUseBlocksEvent,
    BidiTranscriptDeltaEvent,
)

from app.call_lifecycle import CallState
from tests.test_call_lifecycle import Harness


def _audio(content_id="old-audio"):
    return BidiAudioDeltaEvent(
        base64.b64encode(b"\0\0" * 480).decode(), "pcm", 24000, 1, content_id,
    )


def _replace_connection(h):
    h.emit(BidiConnectionRestartEvent("scheduled", turn_interrupted=True), provider=False)
    h.emit(BidiConnectionStartEvent("replacement", "fake"))


def _old_tool_group():
    return BidiToolUseBlocksEvent([
        {"toolUseId": "old-write", "name": "update_due_record", "input": {}},
    ])


async def _complete_farewell(h):
    h.farewell()
    mark = await h.mark()
    assert h.controller.acknowledge_playback(mark, "MZ_fake")
    await h.controller.closed.wait()
    assert len(h.hangups) == len(h.saved) == 1
    assert h.saved[0][1]["termination"]["outcome"] == "farewell_played"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [
    _audio(),
    BidiResponseStartEvent("old-response"),
    BidiTranscriptDeltaEvent("old words", "assistant", "old-transcript"),
])
async def test_sdk_dropped_old_receipt_allows_fresh_farewell(stale):
    h = Harness()
    try:
        h.controller.observe_provider_event(stale)
        # No observe_event: the SDK discarded this superseded reader event.
        _replace_connection(h)
        await h.ready()
        await _complete_farewell(h)
        assert not h.controller._pending_output
    finally:
        await h.finish()


@pytest.mark.asyncio
async def test_late_old_audio_does_not_invalidate_or_satisfy_new_playback_evidence():
    h = Harness()
    old = _audio("audio")  # Reuse the new stream's content ID deliberately.
    try:
        h.controller.observe_provider_event(old)
        _replace_connection(h)
        await h.ready()
        h.farewell(audio="audio")
        mark = await h.mark()
        revision = h.controller._revision
        h.controller.observe_event(old)
        assert h.controller._revision == revision
        assert not h.controller._pending_output
        assert h.controller.acknowledge_playback(mark, "MZ_fake")
        await h.controller.closed.wait()
        assert len(h.hangups) == 1
    finally:
        await h.finish()


@pytest.mark.asyncio
async def test_preregistered_but_undispatched_old_tool_does_not_block_new_ending():
    h = Harness()
    dropped = _old_tool_group()
    try:
        h.controller.observe_provider_event(dropped)
        _replace_connection(h)
        assert "old-write" not in h.controller.tools
        assert h.runtime.pending_write_count == 0
        await h.ready()
        await _complete_farewell(h)
    finally:
        await h.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_after_restart", [False, True])
async def test_actual_write_survives_restart_or_revives_retired_registration(start_after_restart):
    h = Harness()
    release, started = threading.Event(), threading.Event()
    waiter = None
    ending = None
    dropped = _old_tool_group()

    def write():
        started.set()
        assert release.wait(timeout=3)
        return {"saved": True}

    def start_write():
        context = SimpleNamespace(
            tool_use={"toolUseId": "old-write"}, cancel_signal=threading.Event(),
        )
        return asyncio.create_task(h.runtime.execute("update_due_record", write, context))

    try:
        h.controller.observe_provider_event(dropped)
        if start_after_restart:
            _replace_connection(h)
            await h.ready(end_id="superseded-end", begin_id="superseded-begin")
            waiter = start_write()
            assert await asyncio.to_thread(started.wait, 1)
            assert h.controller.state == CallState.ACTIVE
            assert h.controller.summary["reason"] == "resumed_tool_operation"
        else:
            waiter = start_write()
            assert await asyncio.to_thread(started.wait, 1)
            _replace_connection(h)
        assert "old-write" in h.controller.tools
        assert h.controller.tools["old-write"].started
        assert h.runtime.pending_write_count == 1
        h.register(("fresh-end", "end_conversation"))
        ending = asyncio.create_task(h.controller.request_end(tool_use_id="fresh-end"))
        await asyncio.sleep(0)
        assert not ending.done()
        assert not h.hangups
        release.set()
        assert await waiter == {"saved": True}
        h.results("old-write")
        ready = await ending
        assert ready["ready"] is True
        h.results("fresh-end")
        h.register(("fresh-begin", "begin_farewell"))
        grant = await h.controller.begin_farewell(ready["attempt_id"], tool_use_id="fresh-begin")
        assert grant["ready"] is True
        h.results("fresh-begin")
        await _complete_farewell(h)
        assert h.runtime.pending_write_count == 0
    finally:
        release.set()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        if ending is not None:
            await asyncio.gather(ending, return_exceptions=True)
        await h.finish()
