"""Audio transcoding between Twilio Media Streams and Amazon Nova Sonic.

Twilio Media Streams carry base64-encoded G.711 u-law (``audio/x-mulaw``),
8000 Hz, mono. After u-law decode this is signed 16-bit linear PCM.

Nova Sonic consumes signed 16-bit LPCM mono at 16000 Hz and produces signed
16-bit LPCM mono at 24000 Hz.

This module bridges those formats using the stdlib :mod:`audioop` module
(available on Python 3.12; removed in 3.13, which is why the project pins
3.12). ``audioop.ratecv`` is stateful: it returns ``(fragment, state)`` and the
state must be threaded back into the next call so successive chunks resample
continuously without clicks. The :class:`Resampler` class encapsulates that
state so a caller holds one instance per direction per call.
"""

from __future__ import annotations

import audioop
import base64

# Sample widths/rates for the bridge (mono, signed 16-bit throughout).
SAMPLE_WIDTH = 2  # bytes per sample (16-bit)
CHANNELS = 1

TWILIO_RATE = 8000  # G.711 u-law sample rate
NOVA_INPUT_RATE = 16000  # Nova Sonic expects 16 kHz PCM in
NOVA_OUTPUT_RATE = 24000  # Nova Sonic emits 24 kHz PCM out


class Resampler:
    """Stateful linear PCM resampler for one direction of one call.

    Wraps :func:`audioop.ratecv`, persisting the opaque conversion state across
    chunks so a stream of fragments resamples as a single continuous signal.
    """

    def __init__(self, from_rate: int, to_rate: int, width: int = SAMPLE_WIDTH, channels: int = CHANNELS) -> None:
        self.from_rate = from_rate
        self.to_rate = to_rate
        self.width = width
        self.channels = channels
        # Opaque ratecv state; None on first call.
        self._state: object | None = None

    def process(self, pcm: bytes) -> bytes:
        """Resample a chunk of 16-bit PCM, carrying state forward.

        Args:
            pcm: Signed 16-bit little-endian PCM at ``from_rate``.

        Returns:
            Signed 16-bit little-endian PCM at ``to_rate``.
        """
        if not pcm:
            return b""
        converted, self._state = audioop.ratecv(
            pcm,
            self.width,
            self.channels,
            self.from_rate,
            self.to_rate,
            self._state,
        )
        return converted

    def reset(self) -> None:
        """Drop accumulated conversion state (e.g. after an interruption)."""
        self._state = None


def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    """Decode G.711 u-law bytes to signed 16-bit linear PCM (same rate)."""
    if not ulaw:
        return b""
    return audioop.ulaw2lin(ulaw, SAMPLE_WIDTH)


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """Encode signed 16-bit linear PCM to G.711 u-law bytes (same rate)."""
    if not pcm:
        return b""
    return audioop.lin2ulaw(pcm, SAMPLE_WIDTH)


def b64_decode(payload: str) -> bytes:
    """Decode a base64 string (Twilio media payload) to raw bytes."""
    return base64.b64decode(payload)


def b64_encode(raw: bytes) -> str:
    """Encode raw bytes to a base64 ASCII string (Twilio media payload)."""
    return base64.b64encode(raw).decode("ascii")


class TwilioToNovaBridge:
    """Transcode inbound Twilio u-law 8 kHz audio to Nova Sonic PCM 16 kHz.

    Holds the stateful upsampler so successive media frames from one call
    resample continuously.
    """

    def __init__(self) -> None:
        self._resampler = Resampler(TWILIO_RATE, NOVA_INPUT_RATE)

    def process_payload(self, b64_ulaw: str) -> bytes:
        """base64 u-law 8k -> raw PCM16 16k bytes."""
        ulaw = b64_decode(b64_ulaw)
        return self.process_bytes(ulaw)

    def process_bytes(self, ulaw: bytes) -> bytes:
        """raw u-law 8k bytes -> raw PCM16 16k bytes."""
        pcm_8k = ulaw_to_pcm16(ulaw)
        return self._resampler.process(pcm_8k)

    def reset(self) -> None:
        self._resampler.reset()


class NovaToTwilioBridge:
    """Transcode outbound Nova Sonic PCM 24 kHz audio to Twilio u-law 8 kHz.

    Holds the stateful downsampler so successive audio chunks from one call
    resample continuously.
    """

    def __init__(self) -> None:
        self._resampler = Resampler(NOVA_OUTPUT_RATE, TWILIO_RATE)

    def process_bytes(self, pcm_24k: bytes) -> bytes:
        """raw PCM16 24k bytes -> raw u-law 8k bytes."""
        pcm_8k = self._resampler.process(pcm_24k)
        return pcm16_to_ulaw(pcm_8k)

    def process_to_payload(self, pcm_24k: bytes) -> str:
        """raw PCM16 24k bytes -> base64 u-law 8k payload for Twilio."""
        return b64_encode(self.process_bytes(pcm_24k))

    def reset(self) -> None:
        self._resampler.reset()


# ---------------------------------------------------------------------------
# Stateless convenience helpers (useful for one-shot transcoding / tests).
# For a live call prefer the stateful bridge classes above.
# ---------------------------------------------------------------------------


def twilio_ulaw_to_nova_pcm(ulaw_8k: bytes) -> bytes:
    """One-shot: u-law 8k bytes -> PCM16 16k bytes (no persisted state)."""
    pcm_8k = ulaw_to_pcm16(ulaw_8k)
    pcm_16k, _ = audioop.ratecv(pcm_8k, SAMPLE_WIDTH, CHANNELS, TWILIO_RATE, NOVA_INPUT_RATE, None)
    return pcm_16k


def nova_pcm_to_twilio_ulaw(pcm_24k: bytes) -> bytes:
    """One-shot: PCM16 24k bytes -> u-law 8k bytes (no persisted state)."""
    pcm_8k, _ = audioop.ratecv(pcm_24k, SAMPLE_WIDTH, CHANNELS, NOVA_OUTPUT_RATE, TWILIO_RATE, None)
    return pcm16_to_ulaw(pcm_8k)
