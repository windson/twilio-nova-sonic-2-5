"""Strands bi-di bridge between a Twilio Media Streams WebSocket and Nova Sonic.

The heart of the app. A Twilio call delivers base64 u-law 8 kHz audio over a
WebSocket; we transcode to LPCM 16 kHz and feed Nova Sonic through Strands
Custom I/O. Nova's 24 kHz LPCM output is transcoded back to u-law 8 kHz and
streamed to Twilio.

Strands I/O model (verified against strands-agents==1.53.0 installed source):

- ``strands.experimental.bidi.agent.BidiAgent`` runs with
  ``await agent.run(inputs=[BidiInput...], outputs=[BidiOutput...])``.
- ``BidiInput`` / ``BidiOutput`` are ``@runtime_checkable`` Protocols with async
  ``start(agent)``, async ``stop()`` and ``__call__``. Input ``__call__()``
  returns the next ``BidiInputEvent``; Output ``__call__(event)`` consumes one
  ``BidiOutputEvent``.
- Audio in: ``BidiAudioInputEvent(audio=<base64 pcm>, format="pcm",
  sample_rate=16000, channels=1)``.
- Audio out: ``BidiAudioStreamEvent`` with ``.audio`` (base64 pcm),
  ``.sample_rate``, ``.format``, ``.channels``.
- Barge-in: ``BidiInterruptionEvent``.
- Model: ``BidiNovaSonicModel(model_id=..., provider_config={"audio": {...}},
  client_config={"region": ...})`` — voice lives in ``provider_config["audio"]``.

FEAT-003 seams: ``build_agent`` accepts an injected ``tools`` list and
``system_prompt``; :class:`SonicSession` accepts an optional ``metrics`` sink
and exposes ``on_event`` hooks so metrics/turn/latency recording can be layered
on without touching the audio path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol

from strands.experimental.bidi.agent import BidiAgent
from strands.experimental.bidi.models import BidiNovaSonicModel
from strands.experimental.bidi.types.events import (
    BidiAudioInputEvent,
    BidiAudioStreamEvent,
    BidiInterruptionEvent,
    BidiOutputEvent,
)

from app.audio import (
    CHANNELS,
    NOVA_INPUT_RATE,
    NovaToTwilioBridge,
    TwilioToNovaBridge,
    b64_encode,
)
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "tiffany"

# A reasonable default system prompt. FEAT-003 injects the collections prompt.
DEFAULT_SYSTEM_PROMPT = (
    "You are a professional, courteous Accounts Receivable collections voice "
    "agent making an outbound call. Speak naturally and concisely."
)


class MetricsSink(Protocol):
    """Hook surface for FEAT-003 metrics recording.

    Every Nova output event is forwarded to ``record_event`` and lifecycle
    transitions to ``on_session_start`` / ``on_session_end``. Implementations
    are optional; the bridge works with ``metrics=None``.
    """

    def on_session_start(self) -> None:  # pragma: no cover - protocol
        ...

    def record_event(self, event: BidiOutputEvent) -> None:  # pragma: no cover - protocol
        ...

    def on_session_end(self) -> None:  # pragma: no cover - protocol
        ...


def build_model(settings: Settings | None = None, voice: str = DEFAULT_VOICE) -> BidiNovaSonicModel:
    """Construct the Nova Sonic bi-di model from configuration.

    model_id comes from ``NOVA_MODEL_ID`` (default ``amazon.nova-2-5-sonic``);
    the voice (``tiffany``) and audio rates go in ``provider_config["audio"]``;
    the AWS region goes in ``client_config``.
    """
    settings = settings or get_settings()
    return BidiNovaSonicModel(
        model_id=settings.nova_model_id,
        provider_config={
            "audio": {
                "voice": voice,
                "input_rate": NOVA_INPUT_RATE,
                "output_rate": 24000,
                "channels": CHANNELS,
            }
        },
        client_config={"region": settings.aws_region},
    )


def build_agent(
    *,
    model: Any | None = None,
    tools: list[Any] | None = None,
    system_prompt: str | None = None,
    settings: Settings | None = None,
    voice: str = DEFAULT_VOICE,
) -> BidiAgent:
    """Build a :class:`BidiAgent` wired to Nova Sonic.

    Args:
        model: Pre-built model (tests inject a stub); built from config if None.
        tools: Injected tool list (FEAT-003 supplies the collections tool);
            defaults to an empty list.
        system_prompt: Injected system prompt; defaults to a basic prompt.
        settings: Settings override (defaults to :func:`get_settings`).
        voice: Nova Sonic voice id (default ``tiffany``).
    """
    model = model if model is not None else build_model(settings=settings, voice=voice)
    return BidiAgent(
        model=model,
        tools=tools or [],
        system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
    )


class _QueueAudioInput:
    """Strands ``BidiInput``: pulls transcoded PCM frames from an asyncio queue.

    The WebSocket handler decodes/transcodes each Twilio ``media`` frame and
    puts the resulting PCM16-16k bytes on ``queue``. ``__call__`` blocks until a
    frame is available and wraps it in a ``BidiAudioInputEvent``.
    """

    def __init__(self, queue: "asyncio.Queue[bytes | None]") -> None:
        self._queue = queue

    async def start(self, agent: BidiAgent) -> None:  # noqa: D401 - protocol hook
        return None

    async def stop(self) -> None:  # noqa: D401 - protocol hook
        return None

    async def __call__(self) -> BidiAudioInputEvent:
        pcm = await self._queue.get()
        if pcm is None:
            # Sentinel: no more input. Block forever so run() is torn down via
            # agent.stop()/task cancellation rather than emitting bogus audio.
            await asyncio.Future()
        return BidiAudioInputEvent(
            audio=b64_encode(pcm or b""),
            format="pcm",
            sample_rate=NOVA_INPUT_RATE,
            channels=CHANNELS,
        )


class _TwilioAudioOutput:
    """Strands ``BidiOutput``: transcode Nova audio to u-law and send to Twilio.

    On :class:`BidiAudioStreamEvent`, base64-decode Nova's 24 kHz PCM, downsample
    to u-law 8 kHz and hand the base64 payload to ``send_media``. On
    :class:`BidiInterruptionEvent` (barge-in), reset the resampler and call
    ``on_interrupt`` so the handler can flush Twilio playback with a ``clear``
    message. Every event is also forwarded to the optional metrics sink.
    """

    def __init__(
        self,
        send_media: Callable[[str], Awaitable[None]],
        on_interrupt: Callable[[], Awaitable[None]] | None = None,
        metrics: MetricsSink | None = None,
    ) -> None:
        self._send_media = send_media
        self._on_interrupt = on_interrupt
        self._metrics = metrics
        self._out = NovaToTwilioBridge()

    async def start(self, agent: BidiAgent) -> None:  # noqa: D401 - protocol hook
        return None

    async def stop(self) -> None:  # noqa: D401 - protocol hook
        return None

    async def __call__(self, event: BidiOutputEvent) -> None:
        # Metrics seam: forward every event (FEAT-003 maps turns/TTFT/usage).
        if self._metrics is not None:
            try:
                self._metrics.record_event(event)
            except Exception:  # pragma: no cover - metrics must never break audio
                logger.exception("metrics sink raised; ignoring")

        if isinstance(event, BidiAudioStreamEvent):
            import base64

            pcm_24k = base64.b64decode(event.audio)
            payload = self._out.process_to_payload(pcm_24k)
            if payload:
                await self._send_media(payload)

        elif isinstance(event, BidiInterruptionEvent):
            logger.debug("reason=<%s> | barge-in: flushing Twilio playback", event.reason)
            self._out.reset()
            if self._on_interrupt is not None:
                await self._on_interrupt()


class SonicSession:
    """Per-call bridge tying one Twilio stream to one Nova Sonic agent run.

    The WebSocket handler owns one instance per call. It feeds inbound audio via
    :meth:`feed_twilio_audio` and provides ``send_media`` / ``on_interrupt``
    callbacks for the outbound path. :meth:`run` drives ``agent.run`` until the
    call ends or :meth:`close` is invoked.
    """

    def __init__(
        self,
        *,
        send_media: Callable[[str], Awaitable[None]],
        on_interrupt: Callable[[], Awaitable[None]] | None = None,
        tools: list[Any] | None = None,
        system_prompt: str | None = None,
        metrics: MetricsSink | None = None,
        settings: Settings | None = None,
        voice: str = DEFAULT_VOICE,
        model: Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._metrics = metrics
        self._inbound = TwilioToNovaBridge()
        self._queue: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        self._input = _QueueAudioInput(self._queue)
        self._output = _TwilioAudioOutput(send_media, on_interrupt=on_interrupt, metrics=metrics)
        self._agent = build_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            settings=self._settings,
            voice=voice,
        )
        self._run_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def agent(self) -> BidiAgent:
        return self._agent

    async def feed_twilio_audio(self, b64_ulaw_payload: str) -> None:
        """Transcode one Twilio ``media`` payload and enqueue it for Nova."""
        if self._closed:
            return
        pcm = self._inbound.process_payload(b64_ulaw_payload)
        if pcm:
            await self._queue.put(pcm)

    async def run(self) -> None:
        """Run the agent to completion (returns when the call/stream ends)."""
        if self._metrics is not None:
            try:
                self._metrics.on_session_start()
            except Exception:  # pragma: no cover
                logger.exception("metrics on_session_start raised; ignoring")
        try:
            await self._agent.run(inputs=[self._input], outputs=[self._output])
        finally:
            if self._metrics is not None:
                try:
                    self._metrics.on_session_end()
                except Exception:  # pragma: no cover
                    logger.exception("metrics on_session_end raised; ignoring")

    def start(self) -> asyncio.Task[None]:
        """Launch :meth:`run` as a background task and return it."""
        if self._run_task is None:
            self._run_task = asyncio.ensure_future(self.run())
        return self._run_task

    async def close(self) -> None:
        """Gracefully tear down: unblock input, cancel run, drain the queue."""
        if self._closed:
            return
        self._closed = True
        # Wake any pending input get() and signal end-of-input.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:  # pragma: no cover - unbounded queue
            pass
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown best-effort
                pass
        # Drain any residual frames.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                break
