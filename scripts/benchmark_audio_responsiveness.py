#!/usr/bin/env python3
"""Offline audio codec and Strands scheduler benchmark.

Example (Python 3.12 and the repository venv are required):
  .venv/bin/python scripts/benchmark_audio_responsiveness.py \
      --iterations 20

An optional --baseline-ref compares another Git revision extracted into a
temporary directory. Each revision imports in a fresh process
using the same installed SDK. All provider I/O, database writes, media sends,
playback acknowledgements and hangups are synthetic. No AWS/Twilio request is
made. Results are local scheduler/codec timings, NOT live Nova TTFA or AEC tests.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace


def _summary(values):
    ordered = sorted(values)
    return {"samples": len(values), "median_ms": round(statistics.median(values), 4),
            "p95_ms": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 4),
            "max_ms": round(max(values), 4)}


async def _worker(repo: Path, iterations: int):
    sys.path.insert(0, str(repo))
    from strands.bidi.models.model import BidiModel
    from strands.bidi.types.content import BidiMessage
    from strands.bidi.types.events import (
        BidiAudioDeltaEvent, BidiAudioStartEvent, BidiAudioStopEvent,
        BidiConnectionStartEvent, BidiResponseStartEvent, BidiResponseStopEvent,
        BidiToolUseBlocksEvent, BidiTranscriptDeltaEvent, BidiTranscriptStartEvent,
        BidiTranscriptStopEvent,
    )
    from strands.bidi.types.media import AudioDelta
    from strands.types.tools import ToolResultBlock
    from app.audio import NOVA_INPUT_RATE, NOVA_OUTPUT_RATE, TWILIO_RATE
    from app.collections_agent import CallToolRuntime, build_tools
    from app.sonic_bridge import SonicSession
    from app.store import InMemoryStore

    has_controller = (repo / "app/call_lifecycle.py").is_file()
    if has_controller:
        from app.call_lifecycle import CallLifecycleController, TerminationBounds

    class Provider(BidiModel):
        def __init__(self):
            self.events = asyncio.Queue()
            self.connected = asyncio.Event()
            self.lifecycle = None
            self.results = {}
            self.result_events = {}
            self.response_started = {}
            self.active_response = None

        def get_config(self):
            return {"model_id": "offline-responsiveness", "connection": {"auto_restart": False}}

        def update_config(self, **config):
            pass

        async def start(self, **kwargs):
            pass

        async def stop(self):
            pass

        async def receive(self):
            self.events.put_nowait(BidiConnectionStartEvent(connection_id="synthetic", model="nova"))
            while True:
                event = await self.events.get()
                if isinstance(event, BidiResponseStartEvent):
                    self.active_response = event.response_id
                    self.response_started[event.response_id] = time.perf_counter()
                if self.lifecycle is not None:
                    self.lifecycle.observe_provider_event(event)
                yield event
                if isinstance(event, BidiConnectionStartEvent):
                    self.connected.set()

        async def send(self, content):
            if not isinstance(content, BidiMessage):
                return
            results = [block for block in content.content if isinstance(block, ToolResultBlock)]
            if self.lifecycle is not None and results:
                self.lifecycle.results_submitted([block.tool_use_id for block in results])
            for block in results:
                self.results[block.tool_use_id] = block
                self.result_events.setdefault(block.tool_use_id, asyncio.Event()).set()

        async def result(self, identity):
            await asyncio.wait_for(self.result_events.setdefault(identity, asyncio.Event()).wait(), 3)
            block = self.results[identity]
            if block.status != "success":
                raise RuntimeError(f"synthetic tool failed: {identity}")
            return block

    class BlockingStore(InMemoryStore):
        def __init__(self):
            super().__init__()
            self.entered, self.release = threading.Event(), threading.Event()

        def update_due(self, account_id, **changes):
            self.entered.set()
            if not self.release.wait(5):
                raise TimeoutError("benchmark write not released")
            return super().update_due(account_id, **changes)

    response_times, jitters, handshakes = [], [], []
    audio_before_write = 0
    interval, ticks, provider_gap, playback_gap = 0.002, 40, 0.005, 0.020
    pcm = b"\x10\x00" * 480  # exactly 20 ms of 24 kHz PCM

    def group(name, identity, **arguments):
        return BidiToolUseBlocksEvent(tool_uses=[{"name": name, "toolUseId": identity, "input": arguments}])

    def audio(content="audio"):
        return BidiAudioDeltaEvent(audio=base64.b64encode(pcm).decode(), format="pcm",
                                   sample_rate=24000, channels=1, content_id=content)

    for index in range(iterations):
        model, store, runtime = Provider(), BlockingStore(), CallToolRuntime()
        delivered = asyncio.Event()
        first_audio = {}
        acknowledgements = []
        controller = None
        metrics = SimpleNamespace(
            on_session_start=lambda: None, on_session_end=lambda: None,
            record_event=lambda event: None, record_lifecycle_event=lambda event, **fields: None,
        )

        async def send_media(payload):
            # Callback completion is our synthetic socket-delivery boundary.
            if not base64.b64decode(payload):
                raise AssertionError("empty output was delivered")
            first_audio.setdefault(model.active_response, time.perf_counter())
            delivered.set()

        async def no_io(*args):
            pass

        async def hangup():
            return {"confirmed_terminal": True, "status": "completed", "attempts": 1}

        async def send_mark(name):
            async def acknowledge():
                await asyncio.sleep(playback_gap)
                controller.acknowledge_playback(name, "MZ_offline_benchmark")
            acknowledgements.append(asyncio.create_task(acknowledge()))

        options = {}
        if has_controller:
            controller = CallLifecycleController(
                call_sid="CA_offline_benchmark", stream_sid="MZ_offline_benchmark", runtime=runtime,
                metrics=metrics, hangup=hangup, persist=no_io, close_websocket=no_io,
                bounds=TerminationBounds(tools=3, generation=3, playback=3, sdk_cleanup=1, twilio_request=1),
            )
            model.lifecycle = controller
            options["lifecycle"] = controller
        session = SonicSession(
            model=model, send_media=send_media, on_interrupt=no_io, send_mark=send_mark,
            tools=build_tools(store, runtime=runtime), metrics=metrics,
            kickoff_text=None, **options,
        )
        if controller is not None:
            controller.bind_session(session)
        run_task = session.start()
        heartbeat_task = None
        try:
            await asyncio.wait_for(model.connected.wait(), 3)
            model.events.put_nowait(group("update_due_record", "write", account_id="ACC-1001", new_status="paid"))
            if not await asyncio.to_thread(store.entered.wait, 3):
                raise AssertionError("native tool did not enter worker")

            async def heartbeat():
                started = time.perf_counter()
                for tick in range(1, ticks + 1):
                    target = started + tick * interval
                    await asyncio.sleep(max(0, target - time.perf_counter()))
                    jitters.append(max(0, time.perf_counter() - target) * 1000)

            heartbeat_task = asyncio.create_task(heartbeat())
            model.events.put_nowait(BidiResponseStartEvent(response_id="responsiveness"))
            await asyncio.sleep(provider_gap)
            model.events.put_nowait(BidiAudioStartEvent(content_id="audio"))
            model.events.put_nowait(audio())
            await asyncio.wait_for(delivered.wait(), 3)
            elapsed = (first_audio["responsiveness"] - model.response_started["responsiveness"]) * 1000
            response_times.append(elapsed)
            if "write" in model.results or store.release.is_set():
                raise AssertionError("audio test was not performed while the tool write remained blocked")
            audio_before_write += 1
            model.events.put_nowait(BidiAudioStopEvent(content_id="audio"))
            model.events.put_nowait(BidiResponseStopEvent(response_id="responsiveness"))
            await heartbeat_task
            store.release.set()
            await model.result("write")

            if controller is not None:
                # Separate closing measurement: do not mix tool settlement,
                # readiness, provider speech and playback waits into TTFA.
                model.events.put_nowait(group("end_conversation", "end"))
                block = await model.result("end")
                ready = json.loads(block.content[0]["text"])
                if ready.get("ready") is not True:
                    raise AssertionError("ending was not ready")
                model.events.put_nowait(group("begin_farewell", "begin", attempt_id=ready["attempt_id"]))
                begin = await model.result("begin")
                if json.loads(begin.content[0]["text"]).get("ready") is not True:
                    raise AssertionError("farewell was not ready")
                for event in [
                    BidiResponseStartEvent(response_id="closing"),
                    BidiTranscriptStartEvent(role="assistant", content_id="text"),
                    BidiTranscriptDeltaEvent(delta="Thank you for your time. Goodbye.", role="assistant", content_id="text"),
                    BidiAudioStartEvent(content_id="closing-audio"), audio("closing-audio"),
                    BidiAudioStopEvent(content_id="closing-audio"),
                    BidiTranscriptStopEvent(role="assistant", content_id="text"),
                    BidiResponseStopEvent(response_id="closing"),
                ]:
                    model.events.put_nowait(event)
                await asyncio.wait_for(controller.closed.wait(), 4)
                if controller.summary["outcome"] != "farewell_played":
                    raise AssertionError(f"unconfirmed synthetic farewell: {controller.summary['outcome']}")
                handshakes.append(controller.summary["closing_handshake_ms"])
        finally:
            store.release.set()
            if heartbeat_task is not None:
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            if controller is not None:
                controller.transport_closed()
                await asyncio.wait_for(controller.start_close(reason="benchmark_cleanup", outcome="caller_disconnected"), 4)
            else:
                await session.close()
            await asyncio.gather(run_task, *acknowledgements, return_exceptions=True)
            await runtime.drain(close=True)

    return {
        "iterations": iterations, "strands_agents_version": importlib.metadata.version("strands-agents"),
        "sample_rates_hz": {"twilio": TWILIO_RATE, "nova_input": NOVA_INPUT_RATE, "nova_output": NOVA_OUTPUT_RATE},
        "conditions": {"fake_provider_audio_gap_ms": provider_gap * 1000,
                       "heartbeat_interval_ms": interval * 1000, "heartbeat_ticks_per_iteration": ticks,
                       "fake_playback_ack_delay_ms": playback_gap * 1000,
                       "telemetry_export": "disabled", "application_aec": False},
        "response_start_to_delivered_first_audio": _summary(response_times),
        "heartbeat_deadline_lateness": _summary(jitters),
        "audio_delivered_while_write_pending": audio_before_write,
        "closing_handshake": ({"protocol": "native_two_phase_nonce", **_summary(handshakes)}
                              if handshakes else {"protocol": "legacy", "measured": False,
                                                "reason": "different legacy ending contract; no equivalent two-phase metric"}),
    }


def _run_one(repo, iterations):
    environment = dict(os.environ)
    environment.update(OTEL_SDK_DISABLED="true", AWS_EC2_METADATA_DISABLED="true")
    process = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", "--repo", str(repo), "--iterations", str(iterations)],
        text=True, capture_output=True, env=environment, check=False,
    )
    if process.returncode:
        raise RuntimeError(f"offline benchmark failed for {repo}:\n{process.stderr}\n{process.stdout}")
    return json.loads(process.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-ref", help="Git commit to extract and compare without changing the checkout")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if not 1 <= arguments.iterations <= 200:
        parser.error("--iterations must be between 1 and 200")
    repo = arguments.repo.resolve()
    if arguments.worker:
        # This mode is also useful when comparing two preexisting checkouts.
        os.environ["OTEL_SDK_DISABLED"] = "true"
        os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
        result = asyncio.run(_worker(repo, arguments.iterations))
    else:
        revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        dirty = bool(subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip())
        result = {"scope": "offline fake-provider Strands scheduler and codec; not live TTFA, echo detection, or load capacity",
                  "python": sys.version.split()[0], "current_revision": revision, "current_checkout_dirty": dirty}
        if arguments.baseline_ref:
            baseline = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "--verify", f"{arguments.baseline_ref}^{{commit}}"], text=True,
            ).strip()
            archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=tar", baseline])
            with tempfile.TemporaryDirectory(prefix="audio-baseline-") as location:
                with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
                    bundle.extractall(location, filter="data")
                result["baseline_revision"] = baseline
                result["baseline"] = _run_one(Path(location), arguments.iterations)
        result["current"] = _run_one(repo, arguments.iterations)
        if "baseline" in result:
            result["median_delta_ms_current_minus_baseline"] = {
                metric: round(result["current"][metric]["median_ms"] - result["baseline"][metric]["median_ms"], 4)
                for metric in ("response_start_to_delivered_first_audio", "heartbeat_deadline_lateness")
            }
        result["interpretation"] = (
            "Fixed synthetic inputs make the experiment repeatable, not its wall-clock measurements deterministic. "
            "Host scheduling affects small differences; inspect repeated medians and tail values. "
            "The 5 ms fake generation gap and 20 ms playback ACK delay are included in their respective metrics. "
            "Closing latency is reported separately; compared revisions can use different closing protocols."
        )
    rendered = json.dumps(result, indent=2) + "\n"
    if arguments.output:
        arguments.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
