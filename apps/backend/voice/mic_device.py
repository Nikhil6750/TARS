"""Resolves the Windows default recording device via PyAudio's WASAPI host
API specifically.

PyAudio's own "default input device" is resolved through the MME host API,
which on hardware like Intel Smart Sound Technology mic arrays exposes a
raw, unprocessed multi-channel feed straight from the array elements. The
WASAPI host API's default device is the same OS-processed endpoint shown
under Windows Settings > Sound > Input (noise suppression / AEC /
beamforming applied by the audio engine) -- the one the prior native CPAL
capture used. Using MME's default here would be a silent audio-quality
regression, not a neutral choice.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedMicrophone:
    device_index: int
    name: str
    sample_rate: int


def resolve_default_input_device() -> ResolvedMicrophone:
    import pyaudio

    audio = pyaudio.PyAudio()
    try:
        for host_api_index in range(audio.get_host_api_count()):
            host_api = audio.get_host_api_info_by_index(host_api_index)
            if "WASAPI" not in str(host_api.get("name", "")):
                continue
            default_device_index = host_api.get("defaultInputDevice", -1)
            if default_device_index is None or default_device_index < 0:
                break
            device = audio.get_device_info_by_index(default_device_index)
            return ResolvedMicrophone(
                device_index=device["index"],
                name=device["name"],
                sample_rate=int(device["defaultSampleRate"]),
            )

        # Non-Windows / no WASAPI host API: fall back to PyAudio's own default.
        device = audio.get_default_input_device_info()
        return ResolvedMicrophone(
            device_index=device["index"],
            name=device["name"],
            sample_rate=int(device["defaultSampleRate"]),
        )
    finally:
        audio.terminate()
