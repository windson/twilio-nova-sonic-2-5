"""Deterministic tests for the Twilio <-> Nova audio transcoding."""

from __future__ import annotations

import audioop

from app import audio


def _silence_ulaw(n_samples: int) -> bytes:
    """n_samples of u-law encoding 16-bit silence (==0x00 PCM)."""
    pcm = b"\x00\x00" * n_samples  # 16-bit little-endian zeros
    return audioop.lin2ulaw(pcm, audio.SAMPLE_WIDTH)


def test_ulaw_to_pcm16_doubles_sample_count_on_8k_to_16k():
    """u-law 8k -> PCM16 16k roughly doubles the byte length.

    Each u-law byte decodes to one 16-bit sample (2 bytes); upsampling 8k->16k
    doubles the sample count again, so output bytes ~= 4x the u-law byte count.
    """
    n = 160  # one 20ms Twilio frame at 8kHz
    ulaw = _silence_ulaw(n)
    assert len(ulaw) == n

    pcm16_16k = audio.twilio_ulaw_to_nova_pcm(ulaw)

    # 8k PCM16 would be n*2 bytes; resampled to 16k ~ doubles -> n*4 bytes.
    expected = n * audio.SAMPLE_WIDTH * (audio.NOVA_INPUT_RATE // audio.TWILIO_RATE)
    assert abs(len(pcm16_16k) - expected) <= audio.SAMPLE_WIDTH * 2
    assert len(pcm16_16k) % audio.SAMPLE_WIDTH == 0


def test_pcm16_24k_to_ulaw_8k_reduces_length_by_rate_factor():
    """PCM16 24k -> u-law 8k shrinks by the 24k->8k rate and u-law 1-byte width."""
    n = 480  # 20ms at 24kHz
    pcm24k = b"\x00\x00" * n
    ulaw_8k = audio.nova_pcm_to_twilio_ulaw(pcm24k)

    # 24k -> 8k is a 1/3 downsample; u-law is 1 byte/sample.
    expected = n * audio.TWILIO_RATE // audio.NOVA_OUTPUT_RATE
    assert abs(len(ulaw_8k) - expected) <= 2
    # u-law output is valid: round-trips back to PCM16 (2 bytes/sample).
    pcm_back = audioop.ulaw2lin(ulaw_8k, audio.SAMPLE_WIDTH)
    assert len(pcm_back) == len(ulaw_8k) * audio.SAMPLE_WIDTH


def test_base64_boundary_is_lossless():
    """base64 encode/decode at the boundary is lossless."""
    raw = bytes(range(256)) * 3
    assert audio.b64_decode(audio.b64_encode(raw)) == raw


def test_resampler_persists_state_across_chunks():
    """Two consecutive chunks through one Resampler produce continuous output.

    The combined output length of two chunks fed through the stateful resampler
    should match (within rounding) a single resample of the concatenation, which
    a stateless-per-chunk resample would not guarantee.
    """
    chunk = b"\x11\x22" * 160  # 160 samples of nonzero PCM16 @ 8k
    r = audio.Resampler(audio.TWILIO_RATE, audio.NOVA_INPUT_RATE)

    out1 = r.process(chunk)
    out2 = r.process(chunk)
    assert out1  # produced output
    assert out2

    total_stateful = len(out1) + len(out2)

    # Single-shot resample of the concatenation for comparison.
    joined, _ = audioop.ratecv(
        chunk + chunk,
        audio.SAMPLE_WIDTH,
        audio.CHANNELS,
        audio.TWILIO_RATE,
        audio.NOVA_INPUT_RATE,
        None,
    )
    # Stateful chunked output stays sample-aligned and close to the single-shot
    # length (ratecv distributes fractional samples via carried state).
    assert total_stateful % audio.SAMPLE_WIDTH == 0
    assert abs(total_stateful - len(joined)) <= audio.SAMPLE_WIDTH * 2


def test_twilio_to_nova_bridge_roundtrip_payload():
    """Full inbound bridge: base64 u-law payload -> PCM16 16k bytes."""
    ulaw = _silence_ulaw(160)
    payload = audio.b64_encode(ulaw)
    bridge = audio.TwilioToNovaBridge()
    pcm = bridge.process_payload(payload)
    assert len(pcm) > 0
    assert len(pcm) % audio.SAMPLE_WIDTH == 0


def test_nova_to_twilio_bridge_produces_base64_ulaw():
    """Full outbound bridge: PCM16 24k bytes -> base64 u-law payload."""
    pcm24k = b"\x00\x00" * 480
    bridge = audio.NovaToTwilioBridge()
    payload = bridge.process_to_payload(pcm24k)
    # Decodable base64 that decodes to valid u-law.
    decoded = audio.b64_decode(payload)
    assert len(decoded) > 0
    # u-law decodes to 2 bytes per sample.
    assert len(audioop.ulaw2lin(decoded, audio.SAMPLE_WIDTH)) == len(decoded) * audio.SAMPLE_WIDTH


def test_empty_input_is_safe():
    """Empty inputs produce empty outputs without raising."""
    assert audio.ulaw_to_pcm16(b"") == b""
    assert audio.pcm16_to_ulaw(b"") == b""
    assert audio.Resampler(8000, 16000).process(b"") == b""
