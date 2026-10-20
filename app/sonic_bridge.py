"""Strands bi-di bridge between a Twilio Media Streams WebSocket and Nova Sonic.

The heart of the app. A Twilio call delivers base64 u-law 8 kHz audio over a
WebSocket; we transcode to LPCM 16 kHz and feed Nova Sonic through Strands
Custom I/O. Nova's 24 kHz LPCM output is transcoded back to u-law 8 kHz and
streamed to Twilio.

Strands I/O model (verified against the GA strands-agents 1.61.1 ``strands.bidi``
namespace installed source):

- ``strands.bidi.agent.BidiAgent`` runs with
  ``await agent.run(inputs=[InputStream...], outputs=[OutputStream...])``.
- ``InputStream`` / ``OutputStream`` are ``@runtime_checkable`` Protocols with
  async ``start(agent)``, async ``stop()`` and ``__call__``. Input
  ``__call__()`` returns the next ``BidiAgentInput`` (a plain ``str`` for a text
  turn, or an ``AudioDelta`` for audio); Output ``__call__(event)`` consumes one
  ``BidiOutputEvent``.
- Audio in: ``AudioDelta(format="pcm", source={"bytes": <pcm16 bytes>})`` from
  ``strands.bidi.types.media`` (``source`` is the ``AudioSource`` TypedDict).
- Text in: a plain ``str`` (the kickoff user turn).
- Audio out: ``BidiAudioDeltaEvent`` with ``.audio`` (base64 pcm),
  ``.format``, ``.sample_rate``, ``.channels`` and ``.content_id``.
- Barge-in: ``BidiBargeInEvent`` (no ``.reason`` attribute).
- Model: ``BedrockNovaSonicModel(model_id=..., region=<str>, voice="tiffany",
  audio={"input": {"sample_rate": 16000}, "output": {"sample_rate": 24000}})``.
  ``voice`` is now a TOP-LEVEL kwarg; the audio config carries only
  ``sample_rate`` (mono PCM); ``region`` is a plain string kwarg.

FEAT-003 seams: ``build_agent`` accepts an injected ``tools`` list and
``system_prompt``; :class:`SonicSession` accepts an optional ``metrics`` sink
and exposes ``on_event`` hooks so metrics/turn/latency recording can be layered
on without touching the audio path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol

from strands.bidi.agent import BidiAgent
from strands.bidi.models import BedrockNovaSonicModel
from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiAudioStopEvent,
    BidiBargeInEvent,
    BidiOutputEvent,
)
from strands.bidi.types.media import AudioDelta

from app.audio import (
    NOVA_INPUT_RATE,
    TWILIO_RATE,
    NovaToTwilioBridge,
    TwilioToNovaBridge,
)
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "tiffany"

# Bounded safety ceilings for the graceful end-of-call drain (BUG 1). When the
# model calls ``end_conversation`` the agent must let its already-generated
# farewell audio finish playing to the caller BEFORE the Twilio hangup, but it
# must never block forever. ``GRACEFUL_END_AUDIO_STOP_TIMEOUT_S`` bounds the
# wait for Nova to signal the farewell audio stream has stopped generating
# (BidiAudioStopEvent); ``GRACEFUL_END_MAX_DRAIN_S`` bounds the subsequent wait
# for the u-law audio already queued to Twilio to finish playing out. The small
# ``GRACEFUL_END_DRAIN_MARGIN_S`` is added to the computed playback tail so the
# last packet is heard in full before the line drops.
GRACEFUL_END_AUDIO_STOP_TIMEOUT_S = 6.0
GRACEFUL_END_MAX_DRAIN_S = 8.0
GRACEFUL_END_DRAIN_MARGIN_S = 0.4

# A reasonable default system prompt. FEAT-003 injects the collections prompt.
DEFAULT_SYSTEM_PROMPT = (
    "You are a professional, courteous Accounts Receivable collections voice "
    "agent making an outbound call. Speak naturally and concisely."
)

# Priming turn that makes the agent speak FIRST (AI-initiated conversation).
# Nova Sonic does not produce speech from a system prompt alone; it needs an
# initial input turn to trigger the opening response. We send this short
# instruction as a user turn right after the connection starts so Nova greets
# the debtor and states the amount due without waiting for the caller to talk.
DEFAULT_KICKOFF_TEXT = (
    "The call is now connected to the debtor. Begin immediately: greet them by "
    "name, identify yourself as an AI assistant from Accounts Receivable, and "
    "state the exact outstanding amount and due date."
)


class MetricsSink(Protocol):
    """Hook surface for FEAT-003 metrics recording.

    Every Nova output event is forwarded to ``record_event`` and lifecycle
    transitions to ``on_session_start`` / ``on_session_end``. Implementations
    are optional; the bridge works with ``metrics=None``.
    """

    def on_session_start(self) -> None:  # pragma: no cover - protocol
        ...

    def record_event(self, event: "BidiOutputEvent") -> None:  # pragma: no cover - protocol
        ...

    def on_session_end(self) -> None:  # pragma: no cover - protocol
        ...


def build_model(
    settings: Settings | None = None, voice: str = DEFAULT_VOICE
) -> BedrockNovaSonicModel:
    """Construct the Nova Sonic bi-di model from configuration.

    model_id comes from ``NOVA_MODEL_ID`` (default ``amazon.nova-2-5-sonic``);
    the voice (``tiffany``) is a top-level kwarg; the audio config carries only
    the mono-PCM sample rates (input 16 kHz, output 24 kHz); the AWS region is a
    plain ``region=`` string kwarg (mutually exclusive with ``boto_session``).
    """
    settings = settings or get_settings()
    return BedrockNovaSonicModel(
        model_id=settings.nova_model_id,
        region=settings.aws_region,
        voice=voice,
        audio={
            "input": {"sample_rate": NOVA_INPUT_RATE},
            "output": {"sample_rate": 24000},
        },
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
    """Strands ``InputStream``: pulls transcoded PCM frames from an asyncio queue.

    The WebSocket handler decodes/transcodes each Twilio ``media`` frame and
    puts the resulting PCM16-16k bytes on ``queue``. ``__call__`` blocks until a
    frame is available and wraps it in an :class:`AudioDelta` (the GA input
    shape; ``source`` is the ``AudioSource`` TypedDict carrying raw PCM bytes).
    """

    def __init__(self, queue: "asyncio.Queue[bytes | None]") -> None:
        self._queue = queue

    async def start(self, agent: BidiAgent) -> None:  # noqa: D401 - protocol hook
        return None

    async def stop(self) -> None:  # noqa: D401 - protocol hook
        return None

    async def __call__(self) -> AudioDelta:
        pcm = await self._queue.get()
        if pcm is None:
            # Sentinel: no more input. Block forever so run() is torn down via
            # agent.stop()/task cancellation rather than emitting bogus audio.
            await asyncio.Future()
        return AudioDelta(format="pcm", source={"bytes": pcm or b""})


class _KickoffInput:
    """Strands ``InputStream`` that makes the agent open the conversation.

    Nova Sonic only speaks once it receives an input turn; a system prompt by
    itself produces no audio. This input returns a single priming user text turn
    (a plain ``str``, the GA text-input shape) on its first call -- right after
    ``agent.run`` has started the connection -- which triggers Nova's opening
    turn (the greeting + amount due). After that one turn it blocks forever so
    it never injects any further input into the stream.
    """

    def __init__(self, text: str) -> None:
        self._text = text
        self._sent = False

    async def start(self, agent: BidiAgent) -> None:  # noqa: D401 - protocol hook
        return None

    async def stop(self) -> None:  # noqa: D401 - protocol hook
        return None

    async def __call__(self) -> str:
        if self._sent:
            # Priming turn already delivered; never emit again.
            await asyncio.Future()
        self._sent = True
        logger.info("priming AI-first opening turn")
        return self._text


class _TwilioAudioOutput:
    """Strands ``OutputStream``: transcode Nova audio to u-law and send to Twilio.

    On :class:`BidiAudioDeltaEvent`, base64-decode Nova's 24 kHz PCM, downsample
    to u-law 8 kHz and hand the base64 payload to ``send_media``. On
    :class:`BidiBargeInEvent` (barge-in), reset the resampler and call
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
        # --- outbound playback bookkeeping for the graceful end-of-call drain
        # (BUG 1). Twilio plays the u-law frames we send at real time (8 kHz),
        # buffering whatever we hand it, so after we stop sending there is a
        # "tail" of already-queued audio still to be heard. ``_playback_end``
        # is the monotonic clock time at which everything sent so far will have
        # finished playing; ``remaining_playback_s`` reads the outstanding tail.
        # ``_audio_idle`` is set when Nova signals the current audio stream has
        # stopped generating (BidiAudioStopEvent) and cleared as soon as a new
        # audio frame arrives, so the drain can wait for the farewell to finish
        # being produced before measuring the playback tail. None of this runs
        # blocking work on the per-frame path -- it is O(1) bookkeeping.
        self._playback_end: float = 0.0
        self._audio_idle = asyncio.Event()
        self._audio_idle.set()

    async def start(self, agent: BidiAgent) -> None:  # noqa: D401 - protocol hook
        return None

    async def stop(self) -> None:  # noqa: D401 - protocol hook
        return None

    @property
    def audio_idle(self) -> "asyncio.Event":
        """Set while no assistant audio stream is actively generating."""
        return self._audio_idle

    def remaining_playback_s(self) -> float:
        """Seconds of already-sent u-law audio still to play out at Twilio."""
        return max(0.0, self._playback_end - asyncio.get_event_loop().time())

    async def __call__(self, event: BidiOutputEvent) -> None:
        # Metrics seam: forward every event (FEAT-003 maps turns/TTFT/usage).
        if self._metrics is not None:
            try:
                self._metrics.record_event(event)
            except Exception:  # pragma: no cover - metrics must never break audio
                logger.exception("metrics sink raised; ignoring")

        if isinstance(event, BidiAudioDeltaEvent):
            import base64

            pcm_24k = base64.b64decode(event.audio)
            ulaw = self._out.process_bytes(pcm_24k)
            if ulaw:
                # A new audio frame is generating: the stream is no longer idle.
                self._audio_idle.clear()
                # Advance the playback tail by this frame's real-time duration
                # (u-law 8 kHz mono => 1 byte per sample). Anchor from "now" if
                # the previous tail already drained, so silence gaps don't make
                # the estimate run behind the actual Twilio clock.
                now = asyncio.get_event_loop().time()
                frame_s = len(ulaw) / float(TWILIO_RATE)
                self._playback_end = max(self._playback_end, now) + frame_s
                await self._send_media(base64.b64encode(ulaw).decode("ascii"))

        elif isinstance(event, BidiAudioStopEvent):
            # Nova finished GENERATING this audio stream (it may still be
            # playing out at Twilio -- see remaining_playback_s()). Mark idle so
            # the graceful drain can proceed to wait out the playback tail.
            self._audio_idle.set()

        elif isinstance(event, BidiBargeInEvent):
            # GA BidiBargeInEvent carries no .reason attribute.
            logger.debug("barge-in: flushing Twilio playback")
            self._out.reset()
            # Caller interrupted: drop the queued playback tail estimate.
            self._playback_end = 0.0
            self._audio_idle.set()
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
        kickoff_text: str | None = DEFAULT_KICKOFF_TEXT,
    ) -> None:
        self._settings = settings or get_settings()
        self._metrics = metrics
        self._inbound = TwilioToNovaBridge()
        self._queue: "asyncio.Queue[bytes | None]" = asyncio.Queue()
        self._input = _QueueAudioInput(self._queue)
        self._output = _TwilioAudioOutput(send_media, on_interrupt=on_interrupt, metrics=metrics)
        # AI-first conversation: a kickoff input primes Nova to speak first.
        # ``kickoff_text=None`` disables it (e.g. for a caller-initiated flow).
        self._kickoff = _KickoffInput(kickoff_text) if kickoff_text else None
        self._agent = build_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            settings=self._settings,
            voice=voice,
        )
        self._run_task: asyncio.Task[None] | None = None
        self._closed = False
        # Guards the one-shot metrics finalization (on_session_end) so it runs
        # exactly once whether triggered by the graceful end-of-call sequence
        # (before the Twilio hangup) or by the agent run's teardown ``finally``.
        self._metrics_finalized = False
        # Guards the teardown so that EVERY caller of close() -- the
        # end_conversation tool's _end_session() task AND the /media websocket
        # ``finally`` that fires when the hangup disconnects the socket -- waits
        # for the SAME teardown to finish (the agent run is cancelled and its
        # ``finally`` has run metrics.on_session_end(), which computes
        # total_e2e_ms / non_nova_ms and emits the call_end_summary event)
        # before returning. Without this a concurrent second close() used to
        # return immediately (bare ``_closed`` early-return) and let the
        # websocket teardown persist analytics BEFORE on_session_end() had run,
        # so the saved record had a null total_e2e_ms on tool-ended calls.
        self._close_task: asyncio.Task[None] | None = None

    @property
    def agent(self) -> BidiAgent:
        return self._agent

    @property
    def kickoff(self) -> "_KickoffInput | None":
        """The AI-first priming input (``None`` when kickoff is disabled)."""
        return self._kickoff

    async def drain_outbound_audio(
        self,
        *,
        audio_stop_timeout_s: float = GRACEFUL_END_AUDIO_STOP_TIMEOUT_S,
        max_drain_s: float = GRACEFUL_END_MAX_DRAIN_S,
        margin_s: float = GRACEFUL_END_DRAIN_MARGIN_S,
    ) -> None:
        """Wait for the in-flight assistant audio to finish playing to Twilio.

        BUG 1 graceful end-of-call: when the model calls ``end_conversation``
        its closing farewell has usually only just been (or is still being)
        generated, and the transcoded u-law is still draining to Twilio. If we
        hang up immediately the caller is cut off mid-goodbye. This method lets
        that audio finish before the hangup:

        1. Wait (bounded by ``audio_stop_timeout_s``) for Nova to signal the
           current audio stream has STOPPED generating (``BidiAudioStopEvent``
           sets the output's ``audio_idle`` event). This covers the case where
           the farewell is still being produced when the tool fires.
        2. Then wait out the remaining Twilio playback tail -- the u-law we have
           already sent still has to play at real time -- plus a small margin,
           bounded by ``max_drain_s`` so a wrong estimate can never hang the
           teardown.

        It is read-only w.r.t. the audio path (only reads the output stream's
        idle flag + playback estimate) and is the terminal action, so it adds no
        work to the per-frame callback. Safe to call when already drained.
        """
        try:
            if not self._output.audio_idle.is_set():
                try:
                    await asyncio.wait_for(
                        self._output.audio_idle.wait(), timeout=audio_stop_timeout_s
                    )
                except asyncio.TimeoutError:
                    logger.warning("graceful end: audio-stop wait timed out; draining anyway")
            remaining = min(self._output.remaining_playback_s() + margin_s, max_drain_s)
            if remaining > 0:
                logger.info("graceful end: draining %.2fs of farewell audio", remaining)
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:  # pragma: no cover - teardown race
            raise
        except Exception:  # noqa: BLE001 - drain is best-effort
            logger.exception("graceful end: drain failed; proceeding to hangup")

    async def feed_twilio_audio(self, b64_ulaw_payload: str) -> None:
        """Transcode one Twilio ``media`` payload and enqueue it for Nova."""
        if self._closed:
            return
        pcm = self._inbound.process_payload(b64_ulaw_payload)
        if pcm:
            await self._queue.put(pcm)

    def finalize_metrics(self) -> None:
        """Run ``metrics.on_session_end()`` exactly once (idempotent).

        Computes the call-level duration metrics (``total_e2e_ms`` /
        ``non_nova_ms``) and emits the ``call_end_summary`` event. BUG 1: this
        is called explicitly from the graceful end-of-call sequence BEFORE the
        Twilio hangup so the persisted analytics always carry a non-null
        ``total_e2e_ms`` even on the tool-driven end path, and again (as a no-op)
        from ``run()``'s ``finally`` for the normal-hangup path. The guard makes
        the two paths race-free: whichever runs first finalizes, the other is a
        no-op, so the summary is emitted once and the metrics are deterministic.
        """
        if self._metrics is None or self._metrics_finalized:
            return
        self._metrics_finalized = True
        try:
            self._metrics.on_session_end()
        except Exception:  # pragma: no cover
            logger.exception("metrics on_session_end raised; ignoring")

    async def run(self) -> None:
        """Run the agent to completion (returns when the call/stream ends)."""
        if self._metrics is not None:
            try:
                self._metrics.on_session_start()
            except Exception:  # pragma: no cover
                logger.exception("metrics on_session_start raised; ignoring")
        try:
            inputs: list[Any] = [self._input]
            if self._kickoff is not None:
                # Kickoff first so the opening turn is sent as soon as the
                # connection is up, before any caller audio arrives.
                inputs.insert(0, self._kickoff)
            await self._agent.run(inputs=inputs, outputs=[self._output])
        finally:
            # Idempotent: a tool-driven graceful end may have finalized already.
            self.finalize_metrics()

    def start(self) -> asyncio.Task[None]:
        """Launch :meth:`run` as a background task and return it."""
        if self._run_task is None:
            self._run_task = asyncio.ensure_future(self.run())
        return self._run_task

    async def close(self) -> None:
        """Gracefully tear down: unblock input, cancel run, drain the queue.

        Deterministic + idempotent across concurrent callers. The end-of-call
        can be driven from two places at once: the ``end_conversation`` tool's
        ``_end_session()`` task (which hangs up Twilio then calls ``close()``)
        and the ``/media`` websocket ``finally`` (which fires when the Twilio
        hangup disconnects the socket and then persists analytics). BOTH must
        observe a completed teardown -- i.e. the agent run cancelled and its
        ``finally`` having run ``metrics.on_session_end()`` (which computes
        ``total_e2e_ms`` / ``non_nova_ms`` and emits ``call_end_summary``) --
        before control returns, so whichever path saves analytics sees the
        finalized metrics. We therefore run the teardown exactly ONCE and have
        every caller await that single teardown task.
        """
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.ensure_future(self._teardown())
        # All callers (including the one that created the task) await the SAME
        # teardown to completion, so on_session_end() is guaranteed to have run
        # before any caller returns and proceeds to persist analytics.
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:  # pragma: no cover - caller cancelled
            # Do not abort the shared teardown just because this awaiter was
            # cancelled; let the teardown finish for the other caller.
            raise

    async def _teardown(self) -> None:
        """Single-shot teardown body (see :meth:`close`)."""
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
                # The run task's ``finally`` ran metrics.on_session_end() before
                # the CancelledError propagated here, so the call-level duration
                # metrics + call_end_summary event are now computed/emitted.
                pass
        # Drain any residual frames.
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover
                break
