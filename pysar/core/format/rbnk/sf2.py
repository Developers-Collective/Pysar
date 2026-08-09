import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable

import numpy as np

from pysar.core.exceptions import ArchiveDumpCancelled
from pysar.core.model.brbnk import BrbnkData
from pysar.core.format.rwar.brwar import Brwar


@dataclass
class SF2Sample:
    name: str = ""
    start: int = 0
    end: int = 0
    loop_start: int = 0
    loop_end: int = 0
    sample_rate: int = 32000
    original_pitch: int = 60
    pitch_correction: int = 0
    sample_link: int = 0
    sample_type: int = 1  # mono


@dataclass
class SF2Generator:
    oper: int = 0
    amount: int = 0


@dataclass
class SF2Zone:
    generators: list[SF2Generator] = field(default_factory=list)
    key_range: tuple[int, int] = (0, 127)
    vel_range: tuple[int, int] = (0, 127)
    sample_index: int = 0


@dataclass
class SF2Instrument:
    name: str = ""
    zones: list[SF2Zone] = field(default_factory=list)


@dataclass
class SF2Preset:
    name: str = ""
    preset: int = 0
    bank: int = 0
    zones: list[SF2Zone] = field(default_factory=list)


@dataclass
class SF2Data:
    name: str = "Converted Bank"
    samples: list[SF2Sample] = field(default_factory=list)
    sample_data: bytes = b""
    instruments: list[SF2Instrument] = field(default_factory=list)
    presets: list[SF2Preset] = field(default_factory=list)


class SF2Gen:
    """SF2 Generator operator IDs."""
    START_ADDRS_OFFSET = 0
    END_ADDRS_OFFSET = 1
    STARTLOOP_ADDRS_OFFSET = 2
    ENDLOOP_ADDRS_OFFSET = 3
    START_ADDRS_COARSE_OFFSET = 4
    MOD_LFO_TO_PITCH = 5
    VIB_LFO_TO_PITCH = 6
    MOD_ENV_TO_PITCH = 7
    INITIAL_FILTER_FC = 8
    INITIAL_FILTER_Q = 9
    MOD_LFO_TO_FILTER_FC = 10
    MOD_ENV_TO_FILTER_FC = 11
    END_ADDRS_COARSE_OFFSET = 12
    MOD_LFO_TO_VOLUME = 13
    CHORUS_EFFECTS_SEND = 15
    REVERB_EFFECTS_SEND = 16
    PAN = 17
    DELAY_MOD_LFO = 21
    FREQ_MOD_LFO = 22
    DELAY_VIB_LFO = 23
    FREQ_VIB_LFO = 24
    DELAY_MOD_ENV = 25
    ATTACK_MOD_ENV = 26
    HOLD_MOD_ENV = 27
    DECAY_MOD_ENV = 28
    SUSTAIN_MOD_ENV = 29
    RELEASE_MOD_ENV = 30
    KEYNUM_TO_MOD_ENV_HOLD = 31
    KEYNUM_TO_MOD_ENV_DECAY = 32
    DELAY_VOL_ENV = 33
    ATTACK_VOL_ENV = 34
    HOLD_VOL_ENV = 35
    DECAY_VOL_ENV = 36
    SUSTAIN_VOL_ENV = 37
    RELEASE_VOL_ENV = 38
    KEYNUM_TO_VOL_ENV_HOLD = 39
    KEYNUM_TO_VOL_ENV_DECAY = 40
    INSTRUMENT = 41
    KEY_RANGE = 43
    VEL_RANGE = 44
    STARTLOOP_ADDRS_COARSE_OFFSET = 45
    KEYNUM = 46
    VELOCITY = 47
    INITIAL_ATTENUATION = 48
    ENDLOOP_ADDRS_COARSE_OFFSET = 50
    COARSE_TUNE = 51
    FINE_TUNE = 52
    SAMPLE_ID = 53
    SAMPLE_MODES = 54
    SCALE_TUNING = 56
    EXCLUSIVE_CLASS = 57
    OVERRIDING_ROOT_KEY = 58


def _brbnk_adsr_to_sf2_timecents(value: int) -> int:
    """
    Convert BRBNK ADSR value (0-127) to SF2 timecents.

    SF2 timecents: 1200 * log2(seconds)
    BRBNK values are roughly linear in the 0-127 range.
    """
    if value <= 0:
        return -32768  # Instant

    # BRBNK uses a roughly exponential curve
    # Map 0-127 to ~0.001s - ~10s
    import math
    seconds = 0.001 * (10000 ** (value / 127))
    timecents = int(1200 * math.log2(max(0.001, seconds)))
    return max(-12000, min(8000, timecents))


def _brbnk_sustain_to_sf2(value: int) -> int:
    """
    Convert BRBNK sustain (0-127) to SF2 sustain attenuation.

    SF2 sustain is in centibels of attenuation (0 = full, 1000 = -100dB).
    BRBNK sustain 127 = full volume, 0 = silent.
    """
    if value >= 127:
        return 0
    if value <= 0:
        return 1000

    # Convert to centibels
    import math
    db = -20 * math.log10(value / 127)
    return int(min(1000, db * 10))


def _brbnk_pan_to_sf2(value: int) -> int:
    """
    Convert BRBNK pan (0-127, 64=center) to SF2 pan (-500 to 500).
    """
    return int((value - 64) * 500 / 64)


def _brbnk_volume_to_sf2_atten(value: int) -> int:
    """
    Convert BRBNK volume (0-127) to SF2 initial attenuation (centibels).
    """
    if value >= 127:
        return 0
    if value <= 0:
        return 1440  # Maximum attenuation

    import math
    db = -20 * math.log10(value / 127)
    return int(min(1440, db * 10))


def _pitch_to_cents(pitch: float) -> tuple[int, int]:
    """
    Convert BRBNK pitch multiplier to SF2 coarse/fine tune.

    Returns (coarse_semitones, fine_cents).
    """
    import math
    if pitch <= 0:
        return (0, 0)

    cents = int(1200 * math.log2(pitch))
    coarse = cents // 100
    fine = cents % 100
    return coarse, fine


def brbnk_to_sf2(
        brbnk: BrbnkData,
        brwar: Brwar | None = None,
        bank_name: str = "Converted Bank",
        single_zone: tuple[int, int] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
) -> SF2Data:
    """
    Convert a BRBNK to SF2 format.

    Args:
        brbnk: The BRBNK data to convert
        brwar: Optional BRWAR containing the wave data
        bank_name: Name for the SF2 bank
        single_zone: If set, (program, zone_index), export only that zone
            mapped to the full key range 0-127.

    Returns:
        SF2Data ready for writing
    """
    def check_cancelled() -> None:
        if cancel_callback is not None and cancel_callback():
            raise ArchiveDumpCancelled("Archive dump cancelled")

    check_cancelled()
    sf2 = SF2Data(name=bank_name)

    selected_program = None
    selected_zone_index = None
    if single_zone is not None:
        selected_program, selected_zone_index = (int(single_zone[0]), int(single_zone[1]))
        if not 0 <= selected_program < len(brbnk.instruments):
            raise ValueError(f"Cannot export missing program {selected_program}")
        selected_params = brbnk.instruments[selected_program].get_all_inst_params()
        if not 0 <= selected_zone_index < len(selected_params):
            raise ValueError(
                f"Program {selected_program} has {len(selected_params)} zones; "
                f"cannot export zone {selected_zone_index}"
            )

    # Collect all samples from BRWAR
    sample_offset = 0
    sample_data_parts = []
    wave_index_to_sample = {}  # Map BRWAR index to SF2 sample index

    if brwar is not None:
        for i in range(len(brwar)):
            check_cancelled()
            brwav = brwar[i]

            # SF2 sample headers describe one mono stream. Nintendo BRWAVs can
            # contain planar stereo, so deliberately downmix all channels
            # instead of misdeclaring interleaved L/R frames as a mono sample.
            decoded_channels = brwav.decode_channels()
            check_cancelled()
            if len(decoded_channels) == 1:
                pcm = decoded_channels[0]
            else:
                channels = [np.frombuffer(channel, dtype="<i2").astype(np.int32) for channel in decoded_channels]
                frame_count = min(len(channel) for channel in channels)
                mixed = sum(channel[:frame_count] for channel in channels) // len(channels)
                pcm = np.clip(mixed, -32768, 32767).astype("<i2").tobytes()

            # SF2 requires 46 zero samples at end
            pcm += b"\x00" * 92

            # Create sample header
            sample = SF2Sample(
                name=f"sample_{i:04d}",
                start=sample_offset // 2,
                end=(sample_offset + len(pcm) - 92) // 2,
                loop_start=(sample_offset // 2) + (brwav.loop_start if brwav.is_looped else 0),
                # A valid in-sample loop range is required by common SF2
                # readers even when SAMPLE_MODES leaves looping disabled.
                loop_end=(sample_offset + len(pcm) - 92) // 2,
                sample_rate=brwav.sample_rate,
                original_pitch=60,  # Will be overridden per-zone
            )

            sf2.samples.append(sample)
            sample_data_parts.append(pcm)
            wave_index_to_sample[i] = len(sf2.samples) - 1
            sample_offset += len(pcm)

    check_cancelled()
    sf2.sample_data = b"".join(sample_data_parts)

    # SF2 requires sample data to be word-aligned
    if len(sf2.sample_data) % 2:
        sf2.sample_data += b"\x00"

    # Add terminal sample (required by SF2)
    terminal_point = sample_offset // 2
    sf2.samples.append(SF2Sample(
        name="EOS",
        start=terminal_point,
        end=terminal_point,
        loop_start=terminal_point,
        loop_end=terminal_point,
        sample_type=0,
    ))

    # Convert instruments
    for prg_idx, inst in enumerate(brbnk.instruments):
        check_cancelled()
        if selected_program is not None and prg_idx != selected_program:
            continue
        if inst.is_empty():
            continue

        sf2_inst = SF2Instrument(name=f"inst_{prg_idx:03d}")

        # Get all InstParams with their key/velocity info
        params = inst.get_all_inst_params()

        # In single-zone mode, reuse zone 0's root key and volume so exported
        # variations stay consistent.
        single_zone_root_key = None
        single_zone_volume = None
        source_zone_indices = list(range(len(params)))
        if selected_program is not None:
            zone0_param = params[0][0]
            if zone0_param is not None:
                single_zone_root_key = zone0_param.original_key
                single_zone_volume = zone0_param.volume
            params = [params[selected_zone_index]]
            source_zone_indices = [selected_zone_index]

        for source_zone_index, (param, key_range, vel_range) in zip(source_zone_indices, params):
            check_cancelled()
            if param is None:
                continue
            if param.wave_index not in wave_index_to_sample:
                wave_count = 0 if brwar is None else len(brwar)
                raise ValueError(
                    f"Program {prg_idx} zone {source_zone_index} references missing "
                    f"BRWAR wave {param.wave_index} (archive has {wave_count} waves)"
                )

            # Determine key/velocity range
            if single_zone is not None and prg_idx == single_zone[0]:
                # Use the full key range in single-zone exports.
                key_lo, key_hi = 0, 127
            else:
                key_lo, key_hi = key_range if key_range is not None else (0, 127)
            vel_lo, vel_hi = vel_range if vel_range is not None else (0, 127)

            zone = SF2Zone(
                key_range=(key_lo, key_hi),
                vel_range=(vel_lo, vel_hi),
                sample_index=wave_index_to_sample[param.wave_index],
            )

            # Add generators
            zone.generators.append(SF2Generator(
                SF2Gen.KEY_RANGE,
                key_lo | (key_hi << 8)
            ))
            zone.generators.append(SF2Generator(
                SF2Gen.VEL_RANGE,
                vel_lo | (vel_hi << 8)
            ))

            # Keep the zone envelope from the original bank.
            zone.generators.append(SF2Generator(
                SF2Gen.ATTACK_VOL_ENV,
                _brbnk_adsr_to_sf2_timecents(param.attack)
            ))
            zone.generators.append(SF2Generator(
                SF2Gen.HOLD_VOL_ENV,
                _brbnk_adsr_to_sf2_timecents(param.hold)
            ))
            zone.generators.append(SF2Generator(
                SF2Gen.DECAY_VOL_ENV,
                _brbnk_adsr_to_sf2_timecents(param.decay)
            ))
            zone.generators.append(SF2Generator(
                SF2Gen.SUSTAIN_VOL_ENV,
                _brbnk_sustain_to_sf2(param.sustain)
            ))
            zone.generators.append(SF2Generator(
                SF2Gen.RELEASE_VOL_ENV,
                _brbnk_adsr_to_sf2_timecents(param.release)
            ))

            # Pan
            zone.generators.append(SF2Generator(
                SF2Gen.PAN,
                _brbnk_pan_to_sf2(param.pan)
            ))

            # Keep the original zone balance when exporting split samples.
            zone_volume = single_zone_volume if single_zone_volume is not None else param.volume
            zone.generators.append(SF2Generator(
                SF2Gen.INITIAL_ATTENUATION,
                _brbnk_volume_to_sf2_atten(zone_volume)
            ))

            # In single-zone mode, use zone 0's root key for consistent pitch.
            root_key = single_zone_root_key if single_zone_root_key is not None else param.original_key
            zone.generators.append(SF2Generator(
                SF2Gen.OVERRIDING_ROOT_KEY,
                root_key
            ))

            # Pitch/tune
            coarse, fine = _pitch_to_cents(param.pitch)
            if coarse != 0:
                zone.generators.append(SF2Generator(SF2Gen.COARSE_TUNE, coarse))
            if fine != 0:
                zone.generators.append(SF2Generator(SF2Gen.FINE_TUNE, fine))

            # Loop mode is a zone property in SF2. The sample header's loop
            # points alone do not make SoundFont players loop the sample.
            if brwar is not None and 0 <= param.wave_index < len(brwar) and brwar[param.wave_index].is_looped:
                zone.generators.append(SF2Generator(SF2Gen.SAMPLE_MODES, 1))

            # Sample reference (must be last)
            zone.generators.append(SF2Generator(
                SF2Gen.SAMPLE_ID,
                zone.sample_index
            ))

            sf2_inst.zones.append(zone)

        if sf2_inst.zones:
            sf2.instruments.append(sf2_inst)

            # Create preset for this instrument
            preset = SF2Preset(
                name=f"preset_{prg_idx:03d}",
                preset=prg_idx % 128,
                bank=prg_idx // 128,
            )

            # Preset zone references the instrument
            preset_zone = SF2Zone()
            preset_zone.generators.append(SF2Generator(
                SF2Gen.INSTRUMENT,
                len(sf2.instruments) - 1
            ))
            preset.zones.append(preset_zone)

            sf2.presets.append(preset)

    # Add terminal preset (required)
    sf2.presets.append(SF2Preset(name="EOP", preset=255, bank=255))

    # Add terminal instrument (required)
    sf2.instruments.append(SF2Instrument(name="EOI"))

    check_cancelled()
    return sf2


def write_sf2(
    sf2: SF2Data,
    output: BinaryIO,
    cancel_callback: Callable[[], bool] | None = None,
) -> None:
    """Write SF2 data to a binary stream."""

    def check_cancelled() -> None:
        if cancel_callback is not None and cancel_callback():
            raise ArchiveDumpCancelled("Archive dump cancelled")

    def ascii_text(value: str) -> bytes:
        return str(value).encode("ascii", errors="replace")

    def fixed_name(value: str) -> bytes:
        return ascii_text(value)[:19].ljust(20, b"\x00")

    def write_chunk(tag: bytes, data: bytes) -> bytes:
        """Write a RIFF chunk with word-aligned data."""
        chunk = tag + struct.pack("<I", len(data)) + data
        # RIFF chunks must be word-aligned (pad to even)
        if len(data) % 2:
            chunk += b"\x00"
        return chunk

    def pad_to_even(data: bytes) -> bytes:
        if len(data) % 2:
            return data + b"\x00"
        return data

    check_cancelled()
    # Build INFO chunk
    info_parts = []
    info_parts.append(write_chunk(b"ifil", struct.pack("<HH", 2, 1)))  # SF2 v2.1
    info_parts.append(write_chunk(b"isng", pad_to_even(b"EMU8000\x00")))
    info_parts.append(write_chunk(b"INAM", pad_to_even(ascii_text(sf2.name) + b"\x00")))
    info_data = b"".join(info_parts)
    info_chunk = write_chunk(b"LIST", b"INFO" + info_data)

    # Build sdta chunk (sample data)
    smpl_chunk = write_chunk(b"smpl", sf2.sample_data)
    sdta_chunk = write_chunk(b"LIST", b"sdta" + smpl_chunk)

    # Build pdta chunk (preset data)
    pdta_parts = []

    # phdr (preset headers)
    phdr_data = b""
    pbag_index = 0
    for preset in sf2.presets:
        check_cancelled()
        name = fixed_name(preset.name)
        phdr_data += name + struct.pack("<HHHLLL",
                                        preset.preset, preset.bank, pbag_index, 0, 0, 0)
        pbag_index += len(preset.zones)
    pdta_parts.append(write_chunk(b"phdr", phdr_data))

    # pbag (preset zones)
    pbag_data = b""
    pgen_index = 0
    pmod_index = 0
    for preset in sf2.presets:
        check_cancelled()
        for zone in preset.zones:
            pbag_data += struct.pack("<HH", pgen_index, pmod_index)
            pgen_index += len(zone.generators)
    pbag_data += struct.pack("<HH", pgen_index, pmod_index)  # Terminal
    pdta_parts.append(write_chunk(b"pbag", pbag_data))

    # pmod (preset modulators - empty for now)
    pdta_parts.append(write_chunk(b"pmod", struct.pack("<HHHHH", 0, 0, 0, 0, 0)))

    # pgen (preset generators)
    pgen_data = b""
    for preset in sf2.presets:
        check_cancelled()
        for zone in preset.zones:
            for gen in zone.generators:
                pgen_data += struct.pack("<Hh", gen.oper, gen.amount)
    pgen_data += struct.pack("<Hh", 0, 0)  # Terminal
    pdta_parts.append(write_chunk(b"pgen", pgen_data))

    # inst (instruments)
    # Terminal instrument (EOI) must have ibagNdx = total non-terminal ibags
    inst_data = b""
    ibag_index = 0
    for inst in sf2.instruments:
        check_cancelled()
        name = fixed_name(inst.name)
        inst_data += name + struct.pack("<H", ibag_index)
        ibag_index += len(inst.zones)
    pdta_parts.append(write_chunk(b"inst", inst_data))

    # ibag (instrument zones) - only write bags for instruments that have zones
    ibag_data = b""
    igen_index = 0
    imod_index = 0
    for inst in sf2.instruments:
        check_cancelled()
        for zone in inst.zones:
            ibag_data += struct.pack("<HH", igen_index, imod_index)
            igen_index += len(zone.generators)
    ibag_data += struct.pack("<HH", igen_index, imod_index)  # Terminal
    pdta_parts.append(write_chunk(b"ibag", ibag_data))

    # imod (instrument modulators - empty)
    pdta_parts.append(write_chunk(b"imod", struct.pack("<HHHHH", 0, 0, 0, 0, 0)))

    # igen (instrument generators)
    igen_data = b""
    for inst in sf2.instruments:
        check_cancelled()
        for zone in inst.zones:
            for gen in zone.generators:
                igen_data += struct.pack("<Hh", gen.oper, gen.amount)
    igen_data += struct.pack("<Hh", 0, 0)  # Terminal
    pdta_parts.append(write_chunk(b"igen", igen_data))

    # shdr (sample headers) - 46 bytes each: 20 name + 26 data
    # SF2 spec: DWORD start/end/loopstart/loopend/samplerate (5x4=20)
    #           BYTE originalPitch (1), CHAR pitchCorrection (1)
    #           WORD sampleLink (2), WORD sampleType (2) = 26
    shdr_data = b""
    for sample in sf2.samples:
        check_cancelled()
        name = fixed_name(sample.name)
        shdr_data += name + struct.pack("<LLLLLBbHH",
                                        sample.start, sample.end,
                                        sample.loop_start, sample.loop_end,
                                        sample.sample_rate,
                                        sample.original_pitch, sample.pitch_correction,
                                        sample.sample_link, sample.sample_type)
    pdta_parts.append(write_chunk(b"shdr", shdr_data))

    pdta_data = b"".join(pdta_parts)
    pdta_chunk = write_chunk(b"LIST", b"pdta" + pdta_data)

    # Build RIFF wrapper
    sfbk_data = info_chunk + sdta_chunk + pdta_chunk
    riff_data = write_chunk(b"RIFF", b"sfbk" + sfbk_data)

    riff_view = memoryview(riff_data)
    for offset in range(0, len(riff_view), 1024 * 1024):
        check_cancelled()
        output.write(riff_view[offset:offset + 1024 * 1024])
    check_cancelled()


def save_sf2(
    sf2: SF2Data,
    path: str | Path,
    cancel_callback: Callable[[], bool] | None = None,
) -> None:
    """Save SF2 data to a file."""
    with open(path, "wb") as f:
        write_sf2(sf2, f, cancel_callback=cancel_callback)


def verify_sf2_presets(path: str | Path) -> list[tuple[int, int, str]]:
    """
    Read an SF2 file and return a list of (bank, preset, name) tuples
    from its phdr chunk. Useful for verifying the file was written correctly.
    """
    with open(path, "rb") as f:
        data = f.read()

    # Find pdta LIST
    pdta_pos = data.find(b"pdta")
    if pdta_pos < 0:
        return []

    # Find phdr chunk within pdta
    phdr_pos = data.find(b"phdr", pdta_pos)
    if phdr_pos < 0:
        return []

    phdr_size = struct.unpack_from("<I", data, phdr_pos + 4)[0]
    phdr_data = data[phdr_pos + 8: phdr_pos + 8 + phdr_size]

    # Each phdr record is 38 bytes
    presets = []
    for i in range(0, len(phdr_data), 38):
        if i + 38 > len(phdr_data):
            break
        name = phdr_data[i:i+20].split(b"\x00")[0].decode("ascii", errors="replace")
        preset_num, bank_num = struct.unpack_from("<HH", phdr_data, i + 20)
        presets.append((bank_num, preset_num, name))

    return presets
