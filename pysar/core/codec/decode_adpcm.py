"""
ADPCM decoder for Nintendo DSP-ADPCM audio.
"""
from typing import Sequence

import numpy as np
from numba import njit


FRAME_SIZE = 8  # Each ADPCM frame is 8 bytes
PACKET_SAMPLES = 14  # 1 header byte + 7 data bytes = 14 samples


def get_bytes_for_adpcm_samples(samples: int) -> int:
    """Calculate the number of bytes needed for a given sample count."""
    packets = samples // PACKET_SAMPLES
    extra_samples = samples % PACKET_SAMPLES
    extra_bytes = 0

    if extra_samples != 0:
        extra_bytes = (extra_samples // 2) + (extra_samples % 2) + 1

    return FRAME_SIZE * packets + extra_bytes


@njit(cache=True, nogil=True)
def _decode_adpcm_block_jit(
    sample_data: np.ndarray,
    n_samples: int,
    n_chn: int,
    coefs: np.ndarray,
    yn1: int,
    yn2: int,
) -> np.ndarray:
    samples_per_frame = 14
    frame_size = 8

    output = np.zeros(n_samples * n_chn, dtype=np.int16)
    samples_written = 0
    offset = 0

    while samples_written < n_samples:
        header = sample_data[offset]
        predictor = header >> 4
        shift = header & 0x0F

        for i in range(samples_per_frame):
            if samples_written >= n_samples:
                break

            byte_idx = 1 + (i // 2)
            nibble_shift = 4 if (i % 2) == 0 else 0
            nibble_byte = sample_data[offset + byte_idx]
            nibble = (nibble_byte >> nibble_shift) & 0x0F

            # Sign-extend the nibble
            if nibble >= 8:
                nibble -= 16

            # Compute predicted sample
            predicted = (
                (coefs[predictor * 2] * yn1) + (coefs[predictor * 2 + 1] * yn2)
            ) // 2048
            sample = (nibble << shift) + predicted

            # Clamp to int16 range
            if sample > 32767:
                sample = 32767
            elif sample < -32768:
                sample = -32768

            yn2 = yn1
            yn1 = sample

            output[samples_written * n_chn] = sample
            samples_written += 1

        offset += frame_size

    return output


def decode_adpcm_block(
    sample_data: bytes | bytearray,
    n_samples: int,
    n_chn: int,
    coefs: Sequence[int],
    yn1: int,
    yn2: int,
) -> bytes:
    """
    Decode a block of DSP-ADPCM audio to 16-bit PCM.

    Args:
        sample_data: The ADPCM encoded sample data
        n_samples: Number of samples to decode
        n_chn: Number of audio channels
        coefs: ADPCM coefficient matrix (16 values)
        yn1: First history sample
        yn2: Second history sample

    Returns:
        Decoded PCM data as bytes (little-endian signed 16-bit)
    """
    sample_data_np = np.frombuffer(sample_data, dtype=np.uint8)
    coefs_np = np.array(coefs, dtype=np.int16)

    decoded = _decode_adpcm_block_jit(sample_data_np, n_samples, n_chn, coefs_np, yn1, yn2)

    return bytes(decoded.tobytes())


def warm_adpcm_decoder() -> None:
    """Compile the ADPCM decoder before first interactive playback."""
    decode_adpcm_block(bytes(FRAME_SIZE), PACKET_SAMPLES, 1, [0] * 16, 0, 0)
