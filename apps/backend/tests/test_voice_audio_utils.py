from __future__ import annotations

from voice.audio_utils import float32_to_pcm16, pcm16_to_wav, wav_to_pcm16


def test_pcm16_to_wav_roundtrip():
    pcm = (b"\x10\x00\xf0\xff") * 100
    wav = pcm16_to_wav(pcm, sample_rate=16000)
    assert wav[:4] == b"RIFF"
    recovered_pcm, sample_rate = wav_to_pcm16(wav)
    assert recovered_pcm == pcm
    assert sample_rate == 16000


def test_float32_to_pcm16_clips_and_scales():
    import numpy as np

    samples = np.array([0.0, 1.0, -1.0, 2.0, -2.0], dtype=np.float32)
    pcm = float32_to_pcm16(samples)
    values = np.frombuffer(pcm, dtype=np.int16)
    assert values[0] == 0
    assert values[1] == 32767
    assert values[3] == 32767  # clipped
    assert values[4] == -32767  # clipped (via -1.0 clip then scale)
