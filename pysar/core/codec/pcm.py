"""
PCM encoding functions for BRWAV files.

Provides decoding/encoding for PCM8 (unsigned 8-bit) and PCM16 (big-endian 16-bit).
"""
import struct
from typing import Sequence


def encode_pcm16(pcm_samples: Sequence[int], n_samples: int) -> bytes:
    """
    Encode PCM samples to big-endian PCM16 format.

    Args:
        pcm_samples: Signed 16-bit PCM samples (native/little-endian)
        n_samples: Number of samples

    Returns:
        Big-endian PCM16 bytes
    """
    output = bytearray(n_samples * 2)

    for i in range(n_samples):
        sample = pcm_samples[i]
        # Clamp to int16 range
        if sample > 32767:
            sample = 32767
        elif sample < -32768:
            sample = -32768
        struct.pack_into(">h", output, i * 2, sample)

    return bytes(output)


def encode_pcm8(pcm_samples: Sequence[int], n_samples: int) -> bytes:
    """
    Encode PCM samples to unsigned PCM8 format.

    Args:
        pcm_samples: Signed 16-bit PCM samples
        n_samples: Number of samples

    Returns:
        Unsigned PCM8 bytes
    """
    output = bytearray(n_samples)

    for i in range(n_samples):
        sample16 = pcm_samples[i]
        # Clamp to int16 range
        if sample16 > 32767:
            sample16 = 32767
        elif sample16 < -32768:
            sample16 = -32768
        # Convert to unsigned 8-bit
        sample8 = (sample16 >> 8) + 128
        output[i] = sample8 & 0xFF

    return bytes(output)


def decode_pcm8_block(
    sample_data: bytes | bytearray,
    n_samples: int,
    n_chn: int,
) -> bytes:
    """
    Decode PCM8 (unsigned 8-bit) audio to 16-bit PCM.

    Args:
        sample_data: The PCM8 sample data
        n_samples: Number of samples
        n_chn: Number of channels

    Returns:
        Decoded PCM16 data (little-endian signed 16-bit)
    """
    output = bytearray(2 * n_samples * n_chn)

    for i in range(n_samples):
        sample8 = sample_data[i]
        sample16 = (sample8 - 128) << 8
        offset = i * n_chn * 2
        struct.pack_into("<h", output, offset, sample16)

    return bytes(output)


def decode_pcm16_block(
    sample_data: bytes | bytearray,
    n_samples: int,
    n_chn: int,
) -> bytes:
    """
    Decode PCM16 (big-endian) audio to little-endian.

    Args:
        sample_data: The big-endian PCM16 sample data
        n_samples: Number of samples
        n_chn: Number of channels

    Returns:
        Little-endian PCM16 data
    """
    output = bytearray(2 * n_samples * n_chn)

    for i in range(n_samples):
        sample = struct.unpack_from(">h", sample_data, i * 2)[0]
        offset = i * n_chn * 2
        struct.pack_into("<h", output, offset, sample)

    return bytes(output)