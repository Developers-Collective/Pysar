"""
BRSTM-specific ADPCM codec functions.

BRSTM uses block-based ADPCM with canonical coefficients and
history tables for seamless block transitions.

Uses Numba JIT compilation for performance when available.
"""
from typing import Sequence

import numpy as np
from pysar.core.types import CANONICAL_ADPCM_COEFS

try:
    from numba import njit
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False
    def njit(*args, **kwargs):
        def wrapper(fn):
            return fn
        return wrapper


# Nibble to signed value lookup (-8 to 7)
NIB2SIGNED = (0, 1, 2, 3, 4, 5, 6, 7, -8, -7, -6, -5, -4, -3, -2, -1)


def _pad32(x: int) -> int:
    """Align value up to a 32-byte boundary."""
    return (x + 0x1F) & ~0x1F


def adpcm_bytes_for_samples(n_samples: int) -> int:
    """Calculate ADPCM byte count for a given sample count."""
    return ((n_samples + 13) // 14) * 8


@njit(fastmath=True, cache=True, nogil=True)
def _decode_adpcm_channel_nb(
        data_u8: np.ndarray,
        coefs_i16: np.ndarray,
        init_h1: int,
        init_h2: int,
        total_samples: int,
) -> np.ndarray:
    """
    JIT-compiled ADPCM decoder for a single channel.

    Args:
        data_u8: Raw ADPCM bytes for this channel
        coefs_i16: 8x2 coefficient matrix
        init_h1: Initial history sample 1
        init_h2: Initial history sample 2
        total_samples: Number of samples to decode

    Returns:
        Decoded PCM16 samples as a numpy array
    """
    out = np.zeros(total_samples, dtype=np.int16)
    di = 0  # destination index
    si = 0  # source index
    h1 = np.int32(init_h1)
    h2 = np.int32(init_h2)

    c1c2 = coefs_i16.astype(np.int32)
    n = data_u8.size

    while di < total_samples and si + 8 <= n:
        header = data_u8[si]
        si += 1
        scale = 1 << (header & 0x0F)
        pi = (header >> 4) & 0x0F
        if pi > 7:
            pi = 0
        c1 = np.int32(c1c2[pi, 0])
        c2 = np.int32(c1c2[pi, 1])

        for _ in range(7):
            if di >= total_samples:
                break
            b = data_u8[si]
            si += 1
            # High nibble then low nibble
            for k in range(2):
                if di >= total_samples:
                    break
                nib = (b >> 4) & 0x0F if k == 0 else (b & 0x0F)
                # Sign extend nibble
                s = nib if nib < 8 else nib - 16
                pred = (c1 * h1 + c2 * h2 + 1024) >> 11
                sample = pred + s * scale
                # Clamp to int16
                if sample < -32768:
                    sample = -32768
                elif sample > 32767:
                    sample = 32767
                out[di] = np.int16(sample)
                h2 = h1
                h1 = sample
                di += 1

    return out


@njit(fastmath=True, cache=True, nogil=True)
def _decode_adpcm_channel_chunk_nb(
        data_u8: np.ndarray,
        coefs_i16: np.ndarray,
        init_h1: int,
        init_h2: int,
        total_samples: int,
) -> tuple:
    """Decode one frame-aligned chunk and return its ending history."""
    out = np.zeros(total_samples, dtype=np.int16)
    di = 0
    si = 0
    h1 = np.int32(init_h1)
    h2 = np.int32(init_h2)
    c1c2 = coefs_i16.astype(np.int32)
    n = data_u8.size

    while di < total_samples and si + 8 <= n:
        header = data_u8[si]
        si += 1
        scale = 1 << (header & 0x0F)
        pi = (header >> 4) & 0x0F
        if pi > 7:
            pi = 0
        c1 = np.int32(c1c2[pi, 0])
        c2 = np.int32(c1c2[pi, 1])
        for _ in range(7):
            if di >= total_samples:
                break
            b = data_u8[si]
            si += 1
            for k in range(2):
                if di >= total_samples:
                    break
                nib = (b >> 4) & 0x0F if k == 0 else (b & 0x0F)
                signed = nib if nib < 8 else nib - 16
                pred = (c1 * h1 + c2 * h2 + 1024) >> 11
                sample = pred + signed * scale
                if sample < -32768:
                    sample = -32768
                elif sample > 32767:
                    sample = 32767
                out[di] = np.int16(sample)
                h2 = h1
                h1 = sample
                di += 1
    return out, int(h1), int(h2)


def decode_adpcm_channel(
        data: bytes,
        coefs: Sequence[tuple[int, int]],
        hist1: int,
        hist2: int,
        total_samples: int,
) -> np.ndarray:
    """
    Decode ADPCM data for a single channel to PCM16.

    Args:
        data: Raw ADPCM bytes for this channel
        coefs: 8 coefficient pairs
        hist1: Initial history sample 1
        hist2: Initial history sample 2
        total_samples: Number of samples to decode

    Returns:
        Decoded PCM16 samples as a numpy array
    """
    data_u8 = np.frombuffer(data, dtype=np.uint8)
    coefs_arr = np.array(coefs, dtype=np.int16)

    if _HAS_NUMBA:
        return _decode_adpcm_channel_nb(data_u8, coefs_arr, hist1, hist2, total_samples)
    else:
        # Fallback to Python implementation
        return _decode_adpcm_channel_py(data, coefs, hist1, hist2, total_samples)


def decode_adpcm_channel_chunk(
        data: bytes,
        coefs: Sequence[tuple[int, int]],
        hist1: int,
        hist2: int,
        total_samples: int,
) -> tuple[np.ndarray, int, int]:
    """Decode a frame-aligned ADPCM chunk while carrying decoder history."""
    if _HAS_NUMBA:
        return _decode_adpcm_channel_chunk_nb(
            np.frombuffer(data, dtype=np.uint8),
            np.asarray(coefs, dtype=np.int16),
            int(hist1),
            int(hist2),
            int(total_samples),
        )
    pcm = _decode_adpcm_channel_py(data, coefs, hist1, hist2, total_samples)
    if len(pcm):
        end_h1 = int(pcm[-1])
        end_h2 = int(pcm[-2]) if len(pcm) > 1 else int(hist1)
    else:
        end_h1, end_h2 = int(hist1), int(hist2)
    return pcm, end_h1, end_h2


def _decode_adpcm_channel_py(
        data: bytes,
        coefs: Sequence[tuple[int, int]],
        hist1: int,
        hist2: int,
        total_samples: int,
) -> np.ndarray:
    """Pure Python fallback for ADPCM decoding."""
    out = np.zeros(total_samples, dtype=np.int16)
    src = memoryview(data)
    si = 0
    di = 0

    while di < total_samples and si + 8 <= len(src):
        header = src[si]
        si += 1
        scale = 1 << (header & 0x0F)
        pi = (header >> 4) & 0x0F
        if pi > 7:
            pi = 0
        c1, c2 = coefs[pi]

        for _ in range(7):
            if di >= total_samples:
                break
            b = src[si]
            si += 1
            for nib_val in (b >> 4, b & 0x0F):
                if di >= total_samples:
                    break
                nib = NIB2SIGNED[nib_val]
                pred = (c1 * hist1 + c2 * hist2 + 1024) >> 11
                sample = pred + (nib * scale)
                sample = max(-32768, min(32767, sample))
                out[di] = sample
                hist2 = hist1
                hist1 = sample
                di += 1

    return out


@njit(fastmath=True, cache=True, nogil=True)
def _best_scale_and_nibbles(
        frame_i16: np.ndarray,
        c1: int,
        c2: int,
        h1: int,
        h2: int,
) -> tuple:
    """Find best scale and nibbles for a 14-sample frame."""
    best_s = 12
    best_sse = 9.99e30
    best_nib = np.zeros(14, dtype=np.uint8)

    c1 = np.int32(c1)
    c2 = np.int32(c2)

    for s in range(13):
        scale = 1 << s
        sh1 = np.int32(h1)
        sh2 = np.int32(h2)
        ok = True
        sse = 0.0
        nibs = np.zeros(14, dtype=np.uint8)

        for j in range(14):
            x = np.int32(frame_i16[j])
            pred = (c1 * sh1 + c2 * sh2 + 1024) >> 11
            r = x - pred

            # Quantize into [-8..7]
            q = r // scale
            if r < 0 and (r % scale):
                q += 1
            if q < -8 or q > 7:
                ok = False
                break

            xr = pred + q * scale
            e = x - xr
            sse += float(e * e)
            nibs[j] = np.uint8(q & 0xF)
            sh2 = sh1

            # Clamp for stability
            if xr < -32768:
                xr = -32768
            elif xr > 32767:
                xr = 32767
            sh1 = xr

        if ok and (sse < best_sse or (abs(sse - best_sse) < 1e-6 and s < best_s)):
            best_sse = sse
            best_s = s
            best_nib = nibs.copy()
            if s == 0:
                break

    return best_s, best_nib, best_sse


@njit(fastmath=True, cache=True, nogil=True)
def _encode_adpcm_channel_nb(pcm_i16: np.ndarray) -> tuple:
    """
    JIT-compiled ADPCM encoder for a single channel.

    Returns:
        (adpcm_bytes, init_ps, init_h1, init_h2, last_ps, frame_histories)
    """
    N = pcm_i16.size
    frames = (N + 13) // 14
    out = np.zeros(frames * 8, dtype=np.uint8)

    # Histories per frame end (h1, h2)
    fh = np.zeros((frames, 2), dtype=np.int16)

    h1 = np.int32(0)
    h2 = np.int32(0)
    init_ps = np.uint16(0)
    last_ps = np.uint16(0)
    wrote_init = False

    # Canonical coefficients
    coefs = np.array([
        [0x0000, 0x0000],
        [0x0800, 0x0000],
        [0x0400, 0x0400],
        [0x0300, 0x0100],
        [0x0380, 0x0100],
        [0x03C0, 0x0100],
        [0x0400, 0x0000],
        [0x03C0, 0x0040],
    ], dtype=np.int32)

    si = 0
    oi = 0

    for fi in range(frames):
        # Build 14-sample frame
        frame = np.zeros(14, dtype=np.int16)
        cnt = 14 if si + 14 <= N else (N - si)
        if cnt > 0:
            frame[:cnt] = pcm_i16[si:si + cnt]
        si += cnt

        best_s = 12
        best_pi = 0
        best_nib = np.zeros(14, dtype=np.uint8)
        best_sse = 9.99e30

        # Try all 8 predictors
        for pi in range(8):
            c1 = coefs[pi, 0]
            c2 = coefs[pi, 1]
            s, nibs, sse = _best_scale_and_nibbles(frame, c1, c2, h1, h2)
            if (sse < best_sse) or (abs(sse - best_sse) < 1e-6 and s < best_s):
                best_sse = sse
                best_s = s
                best_pi = pi
                best_nib = nibs.copy()

        # Write header byte
        out[oi] = np.uint8(((best_pi & 0xF) << 4) | (best_s & 0xF))
        oi += 1

        # Write 7 bytes of nibbles
        for j in range(0, 14, 2):
            out[oi] = np.uint8(((best_nib[j] & 0xF) << 4) | (best_nib[j + 1] & 0xF))
            oi += 1

        # Update histories by re-synthesizing
        c1 = coefs[best_pi, 0]
        c2 = coefs[best_pi, 1]
        for j in range(14):
            pred = (c1 * h1 + c2 * h2 + 1024) >> 11
            nib = int(best_nib[j])
            q = nib if nib < 8 else nib - 16
            sample = pred + (q << best_s)
            if sample < -32768:
                sample = -32768
            elif sample > 32767:
                sample = 32767
            h2 = h1
            h1 = sample

        fh[fi, 0] = np.int16(h1)
        fh[fi, 1] = np.int16(h2)

        # DSP pred/scale is the encoded frame header byte stored in a u16.
        ps = np.uint16(((best_pi & 0x0F) << 4) | (best_s & 0x0F))
        if not wrote_init:
            init_ps = ps
            wrote_init = True
        last_ps = ps

    # Encoding starts from a silent decoder context.  The first frame's ending
    # history belongs to the next frame/block, never to the stream header.
    init_h1 = np.int16(0)
    init_h2 = np.int16(0)

    return out, init_ps, init_h1, init_h2, last_ps, fh


def encode_adpcm_channel(pcm_samples: np.ndarray) -> tuple:
    """
    Encode a single channel of PCM16 to ADPCM.

    Args:
        pcm_samples: PCM16 samples as numpy int16 array

    Returns:
        Tuple of (adpcm_bytes, init_ps, init_h1, init_h2, last_ps, frame_histories)
    """
    pcm_i16 = np.asarray(pcm_samples, dtype=np.int16)

    if _HAS_NUMBA:
        return _encode_adpcm_channel_nb(pcm_i16)
    else:
        # Try py_func fallback
        if hasattr(_encode_adpcm_channel_nb, 'py_func'):
            return _encode_adpcm_channel_nb.py_func(pcm_i16)
        else:
            raise RuntimeError(
                "Numba not available and no Python fallback for ADPCM encoding. "
                "Please install numba: pip install numba"
            )


@njit(fastmath=True, cache=True, nogil=True)
def _compute_loop_context_nb(
        adpcm_u8: np.ndarray,
        loop_start: int,
        coefs_i16: np.ndarray,
) -> tuple:
    """
    Compute predictor/scale and history at loop start sample.

    Returns:
        (ps, h1, h2) at the loop start position
    """
    si = 0
    pos = 0
    h1 = np.int32(0)
    h2 = np.int32(0)
    ps = np.uint16(0)
    n = adpcm_u8.size
    c12 = coefs_i16.astype(np.int32)

    while pos < loop_start and si + 8 <= n:
        header = adpcm_u8[si]
        si += 1
        s = header & 0x0F
        pi = (header >> 4) & 0x0F
        if pi > 7:
            pi = 0
        c1 = np.int32(c12[pi, 0])
        c2 = np.int32(c12[pi, 1])

        payload_start = si
        si += 7

        for k in range(14):
            if pos == loop_start:
                ps = np.uint16(((pi & 0x0F) << 4) | (s & 0x0F))
                return ps, np.int16(h1), np.int16(h2)

            b = adpcm_u8[payload_start + (k // 2)]
            nib = (b >> 4) & 0x0F if (k % 2) == 0 else (b & 0x0F)
            q = nib if nib < 8 else nib - 16
            pred = (c1 * h1 + c2 * h2 + 1024) >> 11
            sample = pred + (q << s)
            if sample < -32768:
                sample = -32768
            elif sample > 32767:
                sample = 32767
            h2 = h1
            h1 = sample
            pos += 1

    return ps, np.int16(h1), np.int16(h2)


def compute_loop_context(
        adpcm_data: bytes,
        loop_start: int,
        coefs: Sequence[tuple[int, int]] = CANONICAL_ADPCM_COEFS,
) -> tuple[int, int, int]:
    """
    Compute the predictor/scale and history values at the loop start.

    Args:
        adpcm_data: ADPCM bytes for the channel
        loop_start: Loop start sample index
        coefs: Coefficient pairs (default: canonical)

    Returns:
        Tuple of (ps, hist1, hist2) at loop start
    """
    adpcm_u8 = np.frombuffer(adpcm_data, dtype=np.uint8)
    coefs_arr = np.array(coefs, dtype=np.int16)

    if _HAS_NUMBA:
        ps, h1, h2 = _compute_loop_context_nb(adpcm_u8, loop_start, coefs_arr)
    else:
        if hasattr(_compute_loop_context_nb, 'py_func'):
            ps, h1, h2 = _compute_loop_context_nb.py_func(adpcm_u8, loop_start, coefs_arr)
        else:
            # Simplified Python fallback
            ps, h1, h2 = 0, 0, 0

    return int(ps), int(h1), int(h2)
