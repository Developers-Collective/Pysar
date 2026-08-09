"""
Audio codec for NW4R sound formats.
"""
from pysar.core.codec.decode_adpcm import (
    decode_adpcm_block,
    get_bytes_for_adpcm_samples,
    FRAME_SIZE,
    PACKET_SAMPLES,
)

from pysar.core.codec.encode_adpcm import (
    dsp_encode,
    dsp_correlate_coefs,
    dsp_encode_frame,
)

from pysar.core.codec.pcm import (
    encode_pcm8,
    encode_pcm16,
    decode_pcm8_block,
    decode_pcm16_block
)

from pysar.core.codec.brstm_adpcm import (
    decode_adpcm_channel,
    encode_adpcm_channel,
    compute_loop_context,
    adpcm_bytes_for_samples
)

__all__ = [
    # BRWAV ADPCM
    "decode_adpcm_block",
    "decode_pcm8_block",
    "decode_pcm16_block",
    "get_bytes_for_adpcm_samples",
    "dsp_encode",
    "dsp_correlate_coefs",
    "dsp_encode_frame",
    # PCM
    "encode_pcm8",
    "encode_pcm16",
    # BRSTM ADPCM
    "decode_adpcm_channel",
    "encode_adpcm_channel",
    "compute_loop_context",
    "adpcm_bytes_for_samples",
    # Constants
    "FRAME_SIZE",
    "PACKET_SAMPLES",
]
