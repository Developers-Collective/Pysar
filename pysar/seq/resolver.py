from dataclasses import dataclass
from threading import Lock

import numpy as np

from pysar.core.model.brbnk import InstParam, NoteOffType, WaveDataLocationType

_WAVE_PAYLOAD_CACHE: dict[tuple[int, int], tuple[np.ndarray, int, bool, int, int]] = {}
_WAVE_PAYLOAD_CACHE_LOCK = Lock()


def clear_wave_payload_cache() -> None:
    with _WAVE_PAYLOAD_CACHE_LOCK:
        _WAVE_PAYLOAD_CACHE.clear()


@dataclass(slots=True)
class ResolvedRegion:
    program: int
    note: int
    velocity: int
    param: InstParam
    samples: np.ndarray
    sample_rate: int
    is_looped: bool
    loop_start: int
    loop_end: int


@dataclass(slots=True)
class ResolvedRegionInfo:
    program: int
    note: int
    velocity: int
    param: InstParam
    sample_rate: int
    sample_count: int
    is_looped: bool
    loop_start: int
    loop_end: int


def pre_decode_wave_payloads(brwar, wave_indices: set[int] | None = None) -> None:
    total = len(brwar)
    indices = wave_indices if wave_indices is not None else range(total)
    brwar_id = id(brwar)
    for wave_index in indices:
        if wave_index < 0 or wave_index >= total:
            continue
        cache_key = (brwar_id, int(wave_index))
        with _WAVE_PAYLOAD_CACHE_LOCK:
            if cache_key in _WAVE_PAYLOAD_CACHE:
                continue
        # Decode outside the lock so we don't block other threads.
        brwav = brwar[wave_index]
        raw = brwav.decode()
        pcm = np.frombuffer(raw, dtype="<i2")
        if brwav.n_channels > 1:
            pcm = pcm.reshape(-1, brwav.n_channels).mean(axis=1).astype(np.int16)
        samples = np.asarray(pcm, dtype=np.float32) / 32768.0
        loop_start = int(brwav.loop_start) if brwav.is_looped else 0
        loop_end = int(brwav.n_samples)
        payload = (samples, int(brwav.sample_rate), bool(brwav.is_looped), loop_start, loop_end)
        with _WAVE_PAYLOAD_CACHE_LOCK:
            _WAVE_PAYLOAD_CACHE.setdefault(cache_key, payload)


class BankWaveResolver:
    def __init__(self, brbnk, brwar):
        self._brbnk = brbnk
        self._brwar = brwar
        self._cache: dict[int, tuple[np.ndarray, int, bool, int, int]] = {}

    def resolve(self, program: int, note: int, velocity: int) -> ResolvedRegion | None:
        param = self._brbnk.get_inst_param(program, key=note, velocity=velocity)
        if param is None:
            return None
        if param.wave_data_location_type != WaveDataLocationType.INDEX:
            return None
        wave_payload = self._get_wave_payload(param.wave_index)
        if wave_payload is None:
            return None
        samples, sample_rate, is_looped, loop_start, loop_end = wave_payload
        return ResolvedRegion(
            program=program,
            note=note,
            velocity=velocity,
            param=param,
            samples=samples,
            sample_rate=sample_rate,
            is_looped=is_looped,
            loop_start=loop_start,
            loop_end=loop_end,
        )

    def resolve_info(self, program: int, note: int, velocity: int) -> ResolvedRegionInfo | None:
        param = self._brbnk.get_inst_param(program, key=note, velocity=velocity)
        if param is None:
            return None
        if param.wave_data_location_type != WaveDataLocationType.INDEX:
            return None
        if param.wave_index < 0 or param.wave_index >= len(self._brwar):
            return None
        brwav = self._brwar[param.wave_index]
        sample_count = brwav.n_samples
        return ResolvedRegionInfo(
            program=program,
            note=note,
            velocity=velocity,
            param=param,
            sample_rate=int(brwav.sample_rate),
            sample_count=sample_count,
            is_looped=bool(brwav.is_looped),
            loop_start=int(brwav.loop_start) if brwav.is_looped else 0,
            loop_end=sample_count,
        )

    def _get_wave_payload(self, wave_index: int) -> tuple[np.ndarray, int, bool, int, int] | None:
        if wave_index in self._cache:
            return self._cache[wave_index]
        if wave_index < 0 or wave_index >= len(self._brwar):
            return None

        cache_key = (id(self._brwar), int(wave_index))
        with _WAVE_PAYLOAD_CACHE_LOCK:
            cached = _WAVE_PAYLOAD_CACHE.get(cache_key)
            if cached is not None:
                self._cache[wave_index] = cached
                return cached

            brwav = self._brwar[wave_index]
            raw = brwav.decode()
            pcm = np.frombuffer(raw, dtype="<i2")
            if brwav.n_channels > 1:
                pcm = pcm.reshape(-1, brwav.n_channels).mean(axis=1).astype(np.int16)
            # Normalize signed 16-bit PCM into the renderer's -1.0..1.0 float range.
            samples = np.asarray(pcm, dtype=np.float32) / 32768.0
            loop_start = int(brwav.loop_start) if brwav.is_looped else 0
            loop_end = int(brwav.n_samples)
            payload = (samples, int(brwav.sample_rate), bool(brwav.is_looped), loop_start, loop_end)
            _WAVE_PAYLOAD_CACHE[cache_key] = payload
        self._cache[wave_index] = payload
        return payload


def midi_ratio(note: int, original_key: int, pitch: float) -> float:
    base_ratio = 2.0 ** ((note - original_key) / 12.0)
    return max(1.0e-6, float(pitch) * base_ratio)


def note_off_ignores_release(param: InstParam, is_looped: bool) -> bool:
    if param.note_off_type != NoteOffType.IGNORE:
        return False
    return not is_looped
