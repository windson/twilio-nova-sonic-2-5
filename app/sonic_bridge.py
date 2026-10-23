"""Twilio 8 kHz mu-law / Nova Sonic PCM bridge using public Strands Bidi APIs.

The bridge owns provider I/O and its run task. The call lifecycle controller
owns farewell evidence, playback marks, telephone termination, and final call
metrics. Provider events are observed before SDK dispatch; audio delivery is
observed separately after the WebSocket send succeeds.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Protocol

from strands.bidi.agent import BidiAgent
from strands.bidi.models import BedrockNovaSonicModel
from strands.bidi.types.content import BidiContentDelta, BidiMessage
from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiBargeInEvent,
    BidiConnectionRestartEvent,
    BidiConnectionStartEvent,
    BidiOutputEvent,
    BidiUsageEvent,
)
from strands.bidi.types.media import AudioDelta
from strands.types.tools import ToolResultBlock

from app.audio import NOVA_INPUT_RATE, NovaToTwilioBridge, TwilioToNovaBridge
from app.config import Settings, get_settings
from app.languages import POLYGLOT_VOICE

logger = logging.getLogger(__name__)
DEFAULT_VOICE = POLYGLOT_VOICE

DEFAULT_SYSTEM_PROMPT = (
    "You are a professional, courteous Accounts Receivable collections voice "
    "agent making an outbound call. Speak naturally and concisely."
)

# Nova requires an input turn to trigger its AI-first opening response.
DEFAULT_KICKOFF_TEXT = (
    "The call is connected; the answerer's identity is not yet confirmed. "
    "Begin in English with the brief AI introduction and intended-person "
    "question from the system prompt, then wait for their answer. "
    "Discuss account details only after they confirm their identity."
)


class MetricsSink(Protocol):
    """Optional event metrics; final telephone metrics belong to the controller."""

    def record_event(self, event: BidiOutputEvent) -> None: ...


class LifecycleObserver(Protocol):
    """Short synchronous callbacks implemented by the call controller.

    ``observe_provider_event`` stamps events before the SDK queues or dispatches
    them, including registering an entire tool group before any tool starts.
    ``observe_event`` records output consumption, including SDK-generated events.
    These callbacks must not perform blocking I/O.
    """

    def observe_provider_event(self, event: BidiOutputEvent) -> None: ...

    def results_submitted(self, tool_use_ids: list[str]) -> None: ...

    def observe_event(self, event: BidiOutputEvent) -> None: ...

    def output_discarded(self, event: BidiOutputEvent) -> None: ...

    def accept_output(self, event: BidiOutputEvent) -> bool: ...


class SessionClosedError(RuntimeError):
    """A new operation was attempted after the session's irreversible gate."""


class ObservedNovaSonicModel(BedrockNovaSonicModel):
    """Observe only public receive/send; inherit provider recovery and config.

    In particular, this class leaves Strands' 420-second scheduled restart,
    connection replay, audio handling, and late tool-result recovery intact.
    No private SDK state or method is read or overridden.
    """

    lifecycle: LifecycleObserver | None = None
    submissions_closed = False
    usage_sink: Callable[[BidiUsageEvent], None] | None = None

    def bind_lifecycle(self, lifecycle: LifecycleObserver | None) -> None:
        self.lifecycle = lifecycle

    def close_submissions(self) -> None:
        self.submissions_closed = True

    async def receive(self) -> AsyncGenerator[BidiOutputEvent, None]:
        async for event in super().receive():
            # Capture provider usage before output admission/SDK queue fences.
            # Queued or interrupted audio is still model usage. Never wait for
            # another usage event or run cost calculations during teardown.
            if isinstance(event, BidiUsageEvent) and self.usage_sink is not None:
                try:
                    self.usage_sink(event)
                except Exception as exc:
                    logger.warning("usage sink raised (%s)", type(exc).__name__)
            if self.submissions_closed:
                return
            if self.lifecycle is not None:
                # Must precede yield: Strands may schedule tools immediately
                # after enqueueing a tool group, before output consumes it.
                self.lifecycle.observe_provider_event(event)
            yield event

    async def send(self, content: BidiMessage | BidiContentDelta) -> None:
        if self.submissions_closed:
            raise SessionClosedError("model submissions are closed")
        await super().send(content)
        if isinstance(content, BidiMessage) and self.lifecycle is not None:
            tool_use_ids = [
                block.tool_use_id for block in content.content
                if isinstance(block, ToolResultBlock)
            ]
            if tool_use_ids:
                # An SDK ToolResultMessageEvent is emitted BEFORE actual send.
                # Only a successful send is evidence of provider submission.
                # A send already in flight when gated may complete; report it
                # truthfully, leaving terminal-state handling to the controller.
                self.lifecycle.results_submitted(tool_use_ids)


def build_model(
    settings: Settings | None = None,
    voice: str = DEFAULT_VOICE,
    *,
    lifecycle: LifecycleObserver | None = None,
) -> ObservedNovaSonicModel:
    """Construct Nova with the app's established 16 kHz in / 24 kHz out PCM."""
    settings = settings or get_settings()
    options: dict[str, Any] = {}
    if settings.endpointing_sensitivity:
        options["params"] = {"turnDetectionConfiguration": {
            "endpointingSensitivity": settings.endpointing_sensitivity,
        }}
    model = ObservedNovaSonicModel(
        model_id=settings.nova_model_id,
        region=settings.aws_region,
        voice=voice,
        **options,
        audio={
            "input": {"sample_rate": NOVA_INPUT_RATE},
            "output": {"sample_rate": 24000},
        },
    )
    model.bind_lifecycle(lifecycle)
    return model


def build_agent(
    *,
    model: Any | None = None,
    tools: list[Any] | None = None,
    system_prompt: str | None = None,
    settings: Settings | None = None,
    voice: str = DEFAULT_VOICE,
    lifecycle: LifecycleObserver | None = None,
) -> BidiAgent:
    """Build the agent; an injected test model owns its public observation seam."""
    if model is None:
        model = build_model(settings=settings, voice=voice, lifecycle=lifecycle)
    elif isinstance(model, ObservedNovaSonicModel):
        model.bind_lifecycle(lifecycle)
    return BidiAgent(
        model=model,
        tools=tools or [],
        system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
    )


class _QueueAudioInput:
    """Strands custom input containing already-transcoded mono PCM frames."""

    def __init__(self, queue: asyncio.Queue[bytes | None]) -> None:
        self._queue = queue

    async def start(self, agent: BidiAgent) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def __call__(self) -> AudioDelta:
        pcm = await self._queue.get()
        if pcm is None:
            # Cancellation of the owned run task closes the SDK and its inputs.
            # Never turn the sentinel into an empty audio message.
            await asyncio.Future()
        return AudioDelta(format="pcm", source={"bytes": pcm or b""})


class _KickoffInput:
    """Send one priming text turn through the same Nova conversation."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._sent = False

    async def start(self, agent: BidiAgent) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def __call__(self) -> str:
        if self._sent:
            await asyncio.Future()
        self._sent = True
        logger.info("priming AI-first opening turn")
        return self._text


class _TwilioAudioOutput:
    """Transcode and deliver audio; never decide when the telephone may end."""

    def __init__(
        self,
        send_media: Callable[[str], Awaitable[None]],
        on_interrupt: Callable[[], Awaitable[None]] | None = None,
        metrics: MetricsSink | None = None,
        lifecycle: LifecycleObserver | None = None,
    ) -> None:
        self._send_media = send_media
        self._on_interrupt = on_interrupt
        self._metrics = metrics
        self._lifecycle = lifecycle
        self._out = NovaToTwilioBridge()
        self._closed = False
        self.usage_at_provider = False

    def close_output(self) -> None:
        self._closed = True
        self._out.reset()

    async def start(self, agent: BidiAgent) -> None:
        return None

    async def stop(self) -> None:
        self.close_output()

    def _observe(self, event: BidiOutputEvent) -> None:
        if self._lifecycle is not None:
            self._lifecycle.observe_event(event)

    def _discard(self, event: BidiOutputEvent) -> None:
        if self._lifecycle is not None:
            self._lifecycle.output_discarded(event)

    async def __call__(self, event: BidiOutputEvent) -> None:
        if self._closed or (self._lifecycle is not None and not self._lifecycle.accept_output(event)):
            self._discard(event)
            return
        if self._metrics is not None and not (self.usage_at_provider and isinstance(event, BidiUsageEvent)):
            try:
                self._metrics.record_event(event)
            except Exception as exc:  # metrics must never break audio
                logger.warning("metrics sink raised (%s); ignoring", type(exc).__name__)

        if isinstance(event, BidiAudioDeltaEvent):
            ulaw = self._out.process_bytes(base64.b64decode(event.audio))
            if ulaw:
                await self._send_media(base64.b64encode(ulaw).decode("ascii"))
                # Provider production and successful socket delivery are distinct
                # evidence. Failed/dropped media never counts as delivered audio.
                self._observe(event)
            else:
                # Empty/resampling-only frames consume their provider stamp
                # without becoming evidence of media delivered to the caller.
                self._discard(event)
            return

        if isinstance(event, (BidiBargeInEvent, BidiConnectionRestartEvent, BidiConnectionStartEvent)):
            self._out.reset()
        # The controller invalidates marks/response identities before any clear.
        # SDK-generated restart/transcript-block events arrive only on this seam.
        self._observe(event)
        needs_clear = isinstance(event, BidiBargeInEvent) or (
            isinstance(event, BidiConnectionRestartEvent) and event.turn_interrupted
        )
        if needs_clear and not self._closed and self._on_interrupt is not None:
            await self._on_interrupt()


class SonicSession:
    """One call's custom I/O and one SDK-owned bidirectional run.

    ``start`` returns the task the controller supervises. ``close`` tears that
    run down once, even when multiple cleanup paths or cancelled awaiters race.
    Neither provider completion nor cleanup finalizes telephone call metrics.
    """

    def __init__(
        self,
        *,
        send_media: Callable[[str], Awaitable[None]],
        on_interrupt: Callable[[], Awaitable[None]] | None = None,
        send_mark: Callable[[str], Awaitable[None]] | None = None,
        tools: list[Any] | None = None,
        system_prompt: str | None = None,
        metrics: MetricsSink | None = None,
        settings: Settings | None = None,
        voice: str = DEFAULT_VOICE,
        model: Any | None = None,
        kickoff_text: str | None = DEFAULT_KICKOFF_TEXT,
        lifecycle: LifecycleObserver | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._metrics = metrics
        self._send_mark = send_mark
        self._transport_closed = False
        self._output_closed = False
        self._inbound = TwilioToNovaBridge()
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._input = _QueueAudioInput(self._queue)
        self._output = _TwilioAudioOutput(
            send_media, on_interrupt=on_interrupt, metrics=metrics, lifecycle=lifecycle,
        )
        self._kickoff = _KickoffInput(kickoff_text) if kickoff_text else None
        self._agent = build_agent(
            model=model, tools=tools, system_prompt=system_prompt,
            settings=self._settings, voice=voice, lifecycle=lifecycle,
        )
        provider = getattr(self._agent, "model", None)
        if metrics is not None and isinstance(provider, ObservedNovaSonicModel):
            provider.usage_sink = metrics.record_event
            self._output.usage_at_provider = True
        self._run_task: asyncio.Task[None] | None = None
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def agent(self) -> BidiAgent:
        return self._agent

    @property
    def kickoff(self) -> _KickoffInput | None:
        return self._kickoff

    def close_output(self) -> None:
        """Irreversibly fence new media/input before the controller commits end.

        Sends already awaiting transport completion cannot be unsent. No new
        output or model send starts after this synchronous gate is closed.
        """
        self._output_closed = True
        self._output.close_output()
        model = getattr(self._agent, "model", None)
        if isinstance(model, ObservedNovaSonicModel):
            model.close_submissions()

    def notify_transport_closed(self) -> None:
        self._transport_closed = True
        self.close_output()

    async def send_playback_mark(self, name: str) -> None:
        """Send a named Twilio mark; the controller matches acknowledgements."""
        if self._closed or self._output_closed or self._transport_closed:
            raise SessionClosedError("session output is closed")
        if self._send_mark is None:
            raise RuntimeError("playback mark transport is unavailable")
        await self._send_mark(name)

    async def send_recovery_prompt(self, text: str) -> None:
        """Request a recovery turn from the existing Nova conversation."""
        if self._closed or self._output_closed or self._transport_closed:
            raise SessionClosedError("session input is closed")
        await self._agent.send(text)

    async def feed_twilio_audio(self, b64_ulaw_payload: str) -> None:
        if self._closed or self._output_closed or self._transport_closed:
            return
        pcm = self._inbound.process_payload(b64_ulaw_payload)
        if pcm:
            self._queue.put_nowait(pcm)

    async def run(self) -> None:
        """Drive custom I/O; propagate completion/failure to the owning task."""
        if self._closed:
            raise SessionClosedError("session is closed")
        inputs: list[Any] = [self._input]
        if self._kickoff is not None:
            inputs.insert(0, self._kickoff)
        await self._agent.run(inputs=inputs, outputs=[self._output])

    def start(self) -> asyncio.Task[None]:
        if self._closed:
            raise SessionClosedError("session is closed")
        if self._run_task is None:
            self._run_task = asyncio.create_task(self.run(), name="nova-sonic-run")
        return self._run_task

    async def close(self) -> None:
        """Await shared SDK teardown; the controller applies its own timeout."""
        if self._close_task is None:
            self._closed = True
            self.close_output()
            self._close_task = asyncio.create_task(self._teardown(), name="nova-sonic-close")
        # Cancelling one awaiter never cancels cleanup awaited by another path.
        await asyncio.shield(self._close_task)

    async def _teardown(self) -> None:
        self._queue.put_nowait(None)
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                # SDK run() owns input/output/model stop in its finally block.
                # The controller separately observes the original task outcome.
                pass
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                break
        self._inbound.reset()
