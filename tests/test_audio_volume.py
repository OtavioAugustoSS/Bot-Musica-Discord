"""NumpyVolumeTransformer: mesmo resultado do loop por amostra do py-cord."""
import array
from math import floor

import discord
import numpy as np

from core.audio import NumpyVolumeTransformer


class FakeSource(discord.AudioSource):
    def __init__(self, frame: bytes) -> None:
        self.frame = frame

    def read(self) -> bytes:
        return self.frame

    def is_opus(self) -> bool:
        return False


def _reference(frame: bytes, volume: float) -> bytes:
    """Espelho do PCMVolumeTransformer.read() upstream (sem audioop)."""
    volume = min(volume, 2.0)
    samples = array.array("h")
    samples.frombytes(frame)
    for i in range(len(samples)):
        samples[i] = int(floor(min(0x7FFF, max(samples[i] * volume, -0x8000))))
    return samples.tobytes()


def _random_frame() -> bytes:
    rng = np.random.default_rng(42)
    return rng.integers(-32768, 32768, 1920, dtype=np.int16).tobytes()


def test_identity_at_volume_one():
    frame = _random_frame()
    out = NumpyVolumeTransformer(FakeSource(frame), volume=1.0).read()
    assert out == frame


def test_silence_at_volume_zero():
    out = NumpyVolumeTransformer(FakeSource(_random_frame()), volume=0.0).read()
    assert np.frombuffer(out, dtype=np.int16).any() == False  # noqa: E712


def test_matches_reference_within_rounding():
    # float32 vs float64 podem divergir 1 unidade no floor — inaudível
    frame = _random_frame()
    for volume in (0.5, 0.8, 1.7):
        ours = np.frombuffer(
            NumpyVolumeTransformer(FakeSource(frame), volume=volume).read(),
            dtype=np.int16,
        ).astype(np.int32)
        ref = np.frombuffer(_reference(frame, volume), dtype=np.int16).astype(np.int32)
        assert int(np.abs(ours - ref).max()) <= 1, volume


def test_clips_instead_of_wrapping():
    frame = np.array([30000, -30000] * 960, dtype=np.int16).tobytes()
    out = np.frombuffer(
        NumpyVolumeTransformer(FakeSource(frame), volume=2.0).read(), dtype=np.int16
    )
    assert out.max() == 0x7FFF
    assert out.min() == -0x8000


def test_volume_is_capped_at_two():
    frame = _random_frame()
    at_two = NumpyVolumeTransformer(FakeSource(frame), volume=2.0).read()
    at_five = NumpyVolumeTransformer(FakeSource(frame), volume=5.0).read()
    assert at_two == at_five


def test_empty_frame_passthrough():
    assert NumpyVolumeTransformer(FakeSource(b""), volume=0.8).read() == b""
