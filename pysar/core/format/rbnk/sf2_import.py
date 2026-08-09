"""SoundFont 2 import for Wii RBNK/RWAR banks.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, replace
from pathlib import Path

from pysar.core.format.rbnk.sf2 import SF2Gen
from pysar.core.format.rwar import Brwar
from pysar.core.format.rwav import Brwav
from pysar.core.model.brbnk import (
    BrbnkData,
    InstParam,
    Instrument,
    InstrumentRegion,
    RangeTable,
)
from pysar.core.types import AudioCodec


_MAX_SF2_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class _Header:
    name: str
    bag_index: int
    preset: int = 0
    bank: int = 0


@dataclass(frozen=True)
class _Sample:
    name: str
    start: int
    end: int
    loop_start: int
    loop_end: int
    sample_rate: int
    original_pitch: int
    pitch_correction: int
    sample_link: int
    sample_type: int


@dataclass
class _Zone:
    sample_index: int
    key_low: int
    key_high: int
    velocity_low: int
    velocity_high: int
    original_key: int | None
    volume: int
    pan: int
    pitch: float
    attack: int
    decay: int
    sustain: int
    release: int
    hold: int
    looped: bool


@dataclass
class SF2ImportResult:
    bank: BrbnkData
    wave_archive: Brwar
    name: str
    warnings: list[str]


def _name(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("latin-1", errors="replace").strip()


def _chunks(raw: bytes, start: int, end: int):
    cursor = start
    while cursor + 8 <= end:
        tag = raw[cursor:cursor + 4]
        size = struct.unpack_from("<I", raw, cursor + 4)[0]
        data_start = cursor + 8
        data_end = data_start + size
        if data_end > end:
            raise ValueError(f"Truncated SF2 chunk {tag!r}")
        yield tag, raw[data_start:data_end]
        cursor = data_end + (size & 1)
    if cursor < end and any(raw[cursor:end]):
        raise ValueError("Invalid trailing bytes in SF2 chunk list")


def _sf2_lists(raw: bytes) -> tuple[dict[bytes, bytes], dict[bytes, bytes], dict[bytes, bytes]]:
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"sfbk":
        raise ValueError("Not a SoundFont 2 RIFF file")
    riff_size = struct.unpack_from("<I", raw, 4)[0]
    riff_end = min(len(raw), 8 + riff_size)
    if riff_end < 12 or 8 + riff_size > len(raw):
        raise ValueError("Truncated SoundFont RIFF")

    lists: dict[bytes, dict[bytes, bytes]] = {}
    for tag, payload in _chunks(raw, 12, riff_end):
        if tag != b"LIST" or len(payload) < 4:
            continue
        kind = payload[:4]
        lists[kind] = {sub_tag: sub for sub_tag, sub in _chunks(payload, 4, len(payload))}
    try:
        return lists[b"INFO"], lists[b"sdta"], lists[b"pdta"]
    except KeyError as exc:
        raise ValueError(f"SoundFont is missing the {exc.args[0].decode(errors='replace')} list") from exc


def _records(data: bytes, size: int, label: str) -> list[bytes]:
    if len(data) % size:
        raise ValueError(f"Malformed SF2 {label} table")
    return [data[i:i + size] for i in range(0, len(data), size)]


def _parse_headers(pdta: dict[bytes, bytes]) -> tuple[list[_Header], list[_Header]]:
    try:
        phdr_raw = _records(pdta[b"phdr"], 38, "phdr")
        inst_raw = _records(pdta[b"inst"], 22, "inst")
    except KeyError as exc:
        raise ValueError(f"SoundFont is missing {exc.args[0].decode()} data") from exc
    if len(phdr_raw) < 1 or len(inst_raw) < 1:
        raise ValueError("SoundFont has no terminal preset/instrument records")
    presets = []
    for rec in phdr_raw:
        preset, bank, bag = struct.unpack_from("<HHH", rec, 20)
        presets.append(_Header(_name(rec[:20]), bag, preset, bank))
    instruments = [
        _Header(_name(rec[:20]), struct.unpack_from("<H", rec, 20)[0])
        for rec in inst_raw
    ]
    return presets, instruments


def _parse_bags_and_gens(pdta: dict[bytes, bytes], bag_tag: bytes, gen_tag: bytes):
    try:
        bag_records = _records(pdta[bag_tag], 4, bag_tag.decode())
        gen_records = _records(pdta[gen_tag], 4, gen_tag.decode())
    except KeyError as exc:
        raise ValueError(f"SoundFont is missing {exc.args[0].decode()} data") from exc
    bags = [struct.unpack_from("<H", rec, 0)[0] for rec in bag_records]
    gens = [struct.unpack("<Hh", rec) for rec in gen_records]
    if not bags:
        raise ValueError(f"SoundFont {bag_tag.decode()} table is empty")
    return bags, gens


def _header_zones(headers: list[_Header], index: int, bags: list[int], gens: list[tuple[int, int]]):
    start = headers[index].bag_index
    end = headers[index + 1].bag_index
    if not (0 <= start <= end < len(bags)):
        raise ValueError("SoundFont contains invalid bag indexes")
    zones = []
    for bag_index in range(start, end):
        gen_start = bags[bag_index]
        gen_end = bags[bag_index + 1]
        if not (0 <= gen_start <= gen_end <= len(gens)):
            raise ValueError("SoundFont contains invalid generator indexes")
        zones.append(gens[gen_start:gen_end])
    return zones


def _parse_samples(pdta: dict[bytes, bytes]) -> list[_Sample]:
    try:
        records = _records(pdta[b"shdr"], 46, "shdr")
    except KeyError as exc:
        raise ValueError("SoundFont is missing sample headers") from exc
    if not records:
        raise ValueError("SoundFont has no terminal sample header")
    result = []
    for rec in records:
        fields = struct.unpack_from("<LLLLLBbHH", rec, 20)
        result.append(_Sample(_name(rec[:20]), *fields))
    return result


def _warn_once(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _zone_map(generators: list[tuple[int, int]]) -> dict[int, int]:
    """Resolve duplicate generators inside one zone using SF2 last-wins semantics."""
    return {operator: amount for operator, amount in generators}


def _split_global_zone(
        zones: list[list[tuple[int, int]]],
        terminal_operator: int,
        context: str,
        warnings: list[str],
) -> tuple[list[tuple[int, int]], list[list[tuple[int, int]]]]:
    """Return the optional first global zone and the playable local zones.

    SoundFont permits a global zone only in the first bag. Later bags without
    the required terminal generator are ignored by conforming players; warn
    rather than accidentally accumulating them into the global state.
    """
    global_zone: list[tuple[int, int]] = []
    local_zones: list[list[tuple[int, int]]] = []
    for zone_index, zone in enumerate(zones):
        terminal_values = [amount for operator, amount in zone if operator == terminal_operator]
        if not terminal_values:
            if zone_index == 0:
                global_zone = zone
            else:
                _warn_once(
                    warnings,
                    f"Ignored non-first {context} zone without its terminal generator",
                )
            continue
        if not zone or zone[-1][0] != terminal_operator:
            raise ValueError(f"{context.capitalize()} zone terminal generator must be last")
        local_zones.append(zone)
    return global_zone, local_zones


def _packed_range(amount: int) -> tuple[int, int]:
    packed = amount & 0xFFFF
    return packed & 0xFF, (packed >> 8) & 0xFF


_GENERATOR_DEFAULTS = {
    SF2Gen.START_ADDRS_OFFSET: 0,
    SF2Gen.END_ADDRS_OFFSET: 0,
    SF2Gen.STARTLOOP_ADDRS_OFFSET: 0,
    SF2Gen.ENDLOOP_ADDRS_OFFSET: 0,
    SF2Gen.START_ADDRS_COARSE_OFFSET: 0,
    SF2Gen.END_ADDRS_COARSE_OFFSET: 0,
    SF2Gen.STARTLOOP_ADDRS_COARSE_OFFSET: 0,
    SF2Gen.ENDLOOP_ADDRS_COARSE_OFFSET: 0,
    SF2Gen.PAN: 0,
    SF2Gen.ATTACK_VOL_ENV: -12000,
    SF2Gen.HOLD_VOL_ENV: -12000,
    SF2Gen.DECAY_VOL_ENV: -12000,
    SF2Gen.SUSTAIN_VOL_ENV: 0,
    SF2Gen.RELEASE_VOL_ENV: -12000,
    SF2Gen.INITIAL_ATTENUATION: 0,
    SF2Gen.COARSE_TUNE: 0,
    SF2Gen.FINE_TUNE: 0,
    SF2Gen.SCALE_TUNING: 100,
}


def _resolve_generators(
        preset_global: list[tuple[int, int]],
        preset_local: list[tuple[int, int]],
        instrument_global: list[tuple[int, int]],
        instrument_local: list[tuple[int, int]],
) -> dict[int, int]:
    """Apply the SF2 preset/instrument generator hierarchy (§9.4).

    A local zone replaces its global-zone value at the same level. The
    resulting preset value then adds to the instrument value/default. Key and
    velocity ranges intersect, while sample substitution generators come only
    from the instrument level.
    """
    preset = _zone_map(preset_global)
    preset.update(_zone_map(preset_local))
    instrument = _zone_map(instrument_global)
    instrument.update(_zone_map(instrument_local))

    resolved = {
        operator: instrument.get(operator, default) + preset.get(operator, 0)
        for operator, default in _GENERATOR_DEFAULTS.items()
    }
    for operator in (SF2Gen.KEY_RANGE, SF2Gen.VEL_RANGE):
        preset_range = _packed_range(preset.get(operator, 0x7F00))
        instrument_range = _packed_range(instrument.get(operator, 0x7F00))
        low = max(preset_range[0], instrument_range[0])
        high = min(preset_range[1], instrument_range[1])
        resolved[operator] = low | (high << 8)
    for operator in (SF2Gen.SAMPLE_ID, SF2Gen.SAMPLE_MODES, SF2Gen.OVERRIDING_ROOT_KEY):
        if operator in instrument:
            resolved[operator] = instrument[operator]
    return resolved


def _timecents_to_brbnk(value: int) -> int:
    if value <= -32768:
        return 0
    seconds = 2.0 ** (max(-12000, min(8000, value)) / 1200.0)
    scaled = math.log(max(0.001, seconds) / 0.001, 10000) * 127
    return max(0, min(127, int(round(scaled))))


def _attenuation_to_volume(centibels: int) -> int:
    return max(0, min(127, int(round(127 * (10.0 ** (-max(0, centibels) / 200.0))))))


def _zone_from_generators(generators: dict[int, int], warnings: list[str]) -> _Zone | None:
    if SF2Gen.SAMPLE_ID not in generators:
        return None
    sample_index = generators[SF2Gen.SAMPLE_ID] & 0xFFFF
    key_low, key_high = _packed_range(generators[SF2Gen.KEY_RANGE])
    vel_low, vel_high = _packed_range(generators[SF2Gen.VEL_RANGE])
    if key_low > key_high or vel_low > vel_high:
        return None
    for operator in (
        SF2Gen.START_ADDRS_OFFSET,
        SF2Gen.END_ADDRS_OFFSET,
        SF2Gen.STARTLOOP_ADDRS_OFFSET,
        SF2Gen.ENDLOOP_ADDRS_OFFSET,
        SF2Gen.START_ADDRS_COARSE_OFFSET,
        SF2Gen.END_ADDRS_COARSE_OFFSET,
        SF2Gen.STARTLOOP_ADDRS_COARSE_OFFSET,
        SF2Gen.ENDLOOP_ADDRS_COARSE_OFFSET,
    ):
        if generators[operator]:
            raise ValueError(
                "SF2 uses per-zone sample-address offsets, which cannot be represented "
                "faithfully by a BRBNK wave reference"
            )
    if generators[SF2Gen.SCALE_TUNING] != 100:
        raise ValueError("SF2 uses non-standard scale tuning, which BRBNK cannot represent")

    cents = 100 * generators[SF2Gen.COARSE_TUNE] + generators[SF2Gen.FINE_TUNE]
    root_value = generators.get(SF2Gen.OVERRIDING_ROOT_KEY, -1)
    if root_value == -1:
        original_key = None
    elif 0 <= root_value <= 127:
        original_key = root_value
    else:
        original_key = 60
        _warn_once(
            warnings,
            f"Invalid SF2 overridingRootKey {root_value}; used MIDI key 60",
        )
    attenuation = generators[SF2Gen.INITIAL_ATTENUATION]
    pan = generators[SF2Gen.PAN]
    raw_loop_mode = generators.get(SF2Gen.SAMPLE_MODES, 0) & 0xFFFF
    loop_mode = raw_loop_mode & 0x3
    if raw_loop_mode & ~0x3:
        _warn_once(warnings, "Ignored reserved upper bits in SF2 sampleModes")
    if loop_mode == 3:
        raise ValueError(
            "SF2 uses loop-until-release mode; BRWAV loops cannot switch to the sample tail on note-off"
        )
    if loop_mode == 2:
        _warn_once(warnings, "SF2 sampleModes value 2 is unused; imported it as non-looping")
    return _Zone(
        sample_index=sample_index,
        key_low=max(0, min(127, key_low)),
        key_high=max(0, min(127, key_high)),
        velocity_low=max(0, min(127, vel_low)),
        velocity_high=max(0, min(127, vel_high)),
        original_key=original_key,
        volume=_attenuation_to_volume(attenuation),
        pan=max(0, min(127, int(round(64 + pan * 64 / 500)))),
        pitch=max(1 / 16, min(16.0, 2.0 ** (cents / 1200.0))),
        attack=_timecents_to_brbnk(generators[SF2Gen.ATTACK_VOL_ENV]),
        decay=_timecents_to_brbnk(generators[SF2Gen.DECAY_VOL_ENV]),
        sustain=_attenuation_to_volume(generators[SF2Gen.SUSTAIN_VOL_ENV]),
        release=_timecents_to_brbnk(generators[SF2Gen.RELEASE_VOL_ENV]),
        hold=_timecents_to_brbnk(generators[SF2Gen.HOLD_VOL_ENV]),
        looped=loop_mode == 1,
    )


def _param(zone: _Zone, wave_index: int) -> InstParam:
    return InstParam(
        wave_index=wave_index,
        attack=zone.attack,
        decay=zone.decay,
        sustain=zone.sustain,
        release=zone.release,
        hold=zone.hold,
        original_key=60 if zone.original_key is None else zone.original_key,
        volume=zone.volume,
        pan=zone.pan,
        pitch=zone.pitch,
    )


def _partition(zones: list[_Zone], sample_map: dict[tuple[int, bool], int]) -> InstrumentRegion:
    if not zones:
        return InstrumentRegion.null()
    key_points = {0, 128}
    for zone in zones:
        key_points.update((zone.key_low, zone.key_high + 1))
    key_points = sorted(key_points)
    key_regions: list[InstrumentRegion] = []
    key_bounds: list[int] = []

    for key_low, key_end in zip(key_points, key_points[1:]):
        key_high = key_end - 1
        candidates = [z for z in zones if z.key_low <= key_low and z.key_high >= key_high]
        velocity_points = {0, 128}
        for zone in candidates:
            velocity_points.update((zone.velocity_low, zone.velocity_high + 1))
        velocity_points = sorted(velocity_points)
        velocity_regions: list[InstrumentRegion] = []
        velocity_bounds: list[int] = []
        for vel_low, vel_end in zip(velocity_points, velocity_points[1:]):
            vel_high = vel_end - 1
            matching = [
                z for z in candidates
                if z.velocity_low <= vel_low and z.velocity_high >= vel_high
            ]
            if len(matching) > 1:
                raise ValueError(
                    "SF2 contains layered zones; BRBNK can select only one sample "
                    f"at key {key_low}, velocity {vel_low}"
                )
            if matching:
                zone = matching[0]
                velocity_regions.append(InstrumentRegion.direct(
                    _param(zone, sample_map[(zone.sample_index, zone.looped)])
                ))
            else:
                velocity_regions.append(InstrumentRegion.null())
            velocity_bounds.append(vel_high)

        if len(velocity_regions) == 1 and not velocity_regions[0].is_null():
            key_regions.append(velocity_regions[0])
        elif all(region.is_null() for region in velocity_regions):
            key_regions.append(InstrumentRegion.null())
        else:
            key_regions.append(InstrumentRegion.range(RangeTable(velocity_bounds, velocity_regions)))
        key_bounds.append(key_high)

    if len(key_regions) == 1 and not key_regions[0].is_null():
        return key_regions[0]
    return InstrumentRegion.range(RangeTable(key_bounds, key_regions))


def _sample_partner(sample_index: int, samples: list[_Sample], warnings: list[str]) -> int | None:
    sample = samples[sample_index]
    if sample.sample_type & 0x8000:
        raise ValueError(f"SF2 sample {sample.name!r} is a ROM sample, which cannot be imported")
    sample_type = sample.sample_type & 0x7FFF
    if sample_type == 1:
        return None
    if sample_type not in (2, 4, 8):
        raise ValueError(f"SF2 sample {sample.name!r} has unsupported sample type {sample_type}")
    if not 0 <= sample.sample_link < len(samples) - 1:
        raise ValueError(f"SF2 sample {sample.name!r} has an invalid linked-sample index")
    linked = samples[sample.sample_link]
    if linked.sample_type & 0x8000:
        raise ValueError(f"Linked SF2 sample {linked.name!r} is a ROM sample, which cannot be imported")
    linked_type = linked.sample_type & 0x7FFF
    expected_type = {2: 4, 4: 2, 8: 8}[sample_type]
    if linked_type != expected_type or linked.sample_link != sample_index:
        raise ValueError(f"SF2 sample {sample.name!r} has a non-reciprocal or incompatible sample link")
    if linked.end - linked.start != sample.end - sample.start or linked.sample_rate != sample.sample_rate:
        raise ValueError(f"Linked SF2 samples {sample.name!r} and {linked.name!r} differ in length or rate")
    if sample_type == 8:
        _warn_once(warnings, f"Ignored linked-sample relationship for mono sample {sample.name!r}")
        return None
    return sample.sample_link


def _stereo_zone_signature(zone: _Zone) -> tuple:
    return (
        zone.key_low,
        zone.key_high,
        zone.velocity_low,
        zone.velocity_high,
        zone.original_key,
        zone.volume,
        zone.pitch,
        zone.attack,
        zone.decay,
        zone.sustain,
        zone.release,
        zone.hold,
        zone.looped,
    )


def _collapse_stereo_zones(
        zones: list[_Zone],
        samples: list[_Sample],
        warnings: list[str],
) -> list[_Zone]:
    """Collapse conventional reciprocal L/R zones before layer validation."""
    collapsed: list[_Zone] = []
    consumed: set[int] = set()
    for index, zone in enumerate(zones):
        if index in consumed:
            continue
        partner_sample = _sample_partner(zone.sample_index, samples, warnings)
        partner_zone_index = None
        if partner_sample is not None:
            signature = _stereo_zone_signature(zone)
            for candidate_index in range(index + 1, len(zones)):
                candidate = zones[candidate_index]
                if (
                    candidate_index not in consumed
                    and candidate.sample_index == partner_sample
                    and _stereo_zone_signature(candidate) == signature
                ):
                    partner_zone_index = candidate_index
                    break
        if partner_zone_index is None:
            collapsed.append(zone)
            continue
        partner = zones[partner_zone_index]
        consumed.add(partner_zone_index)
        collapsed.append(replace(zone, pan=max(0, min(127, int(round((zone.pan + partner.pan) / 2))))))
        _warn_once(
            warnings,
            "Collapsed reciprocal stereo SF2 zones and downmixed their linked samples to mono",
        )
    return collapsed


def _sample_pcm(
        sample_index: int,
        samples: list[_Sample],
        smpl: bytes,
        warnings: list[str],
) -> list[int]:
    sample = samples[sample_index]
    total_points = len(smpl) // 2
    if not (0 <= sample.start < sample.end <= total_points):
        raise ValueError(f"SF2 sample {sample.name!r} has invalid sample bounds")
    count = sample.end - sample.start
    primary = list(struct.unpack_from(f"<{count}h", smpl, sample.start * 2))
    partner_index = _sample_partner(sample_index, samples, warnings)
    if partner_index is not None:
        linked = samples[partner_index]
        if not 0 <= linked.start < linked.end <= total_points:
            raise ValueError(f"Linked SF2 sample {linked.name!r} has invalid sample bounds")
        secondary = struct.unpack_from(f"<{count}h", smpl, linked.start * 2)
        primary = [max(-32768, min(32767, (a + b) // 2)) for a, b in zip(primary, secondary)]
        _warn_once(warnings, f"Stereo sample {sample.name!r} was downmixed to mono")
    return primary


def load_sf2(source: str | Path | bytes) -> SF2ImportResult:
    """Read an SF2 and convert it to a matching BRBNK + BRWAR pair."""
    if isinstance(source, bytes):
        raw = source
        source_name = "Imported SoundFont"
    else:
        path = Path(source)
        if path.stat().st_size > _MAX_SF2_BYTES:
            raise ValueError("SoundFont is larger than the 512 MiB import limit")
        raw = path.read_bytes()
        source_name = path.stem
    if len(raw) > _MAX_SF2_BYTES:
        raise ValueError("SoundFont is larger than the 512 MiB import limit")

    info, sdta, pdta = _sf2_lists(raw)
    presets, instruments = _parse_headers(pdta)
    p_bags, p_gens = _parse_bags_and_gens(pdta, b"pbag", b"pgen")
    i_bags, i_gens = _parse_bags_and_gens(pdta, b"ibag", b"igen")
    samples = _parse_samples(pdta)
    smpl = sdta.get(b"smpl")
    if smpl is None or len(smpl) % 2:
        raise ValueError("SoundFont has no valid 16-bit sample-data chunk")
    if b"INAM" in info:
        source_name = _name(info[b"INAM"]) or source_name

    warnings: list[str] = []
    address_operators = {
        SF2Gen.START_ADDRS_OFFSET,
        SF2Gen.END_ADDRS_OFFSET,
        SF2Gen.STARTLOOP_ADDRS_OFFSET,
        SF2Gen.ENDLOOP_ADDRS_OFFSET,
        SF2Gen.START_ADDRS_COARSE_OFFSET,
        SF2Gen.END_ADDRS_COARSE_OFFSET,
        SF2Gen.STARTLOOP_ADDRS_COARSE_OFFSET,
        SF2Gen.ENDLOOP_ADDRS_COARSE_OFFSET,
    }
    supported_operators = {
        *address_operators,
        SF2Gen.PAN,
        SF2Gen.ATTACK_VOL_ENV,
        SF2Gen.HOLD_VOL_ENV,
        SF2Gen.DECAY_VOL_ENV,
        SF2Gen.SUSTAIN_VOL_ENV,
        SF2Gen.RELEASE_VOL_ENV,
        SF2Gen.INSTRUMENT,
        SF2Gen.KEY_RANGE,
        SF2Gen.VEL_RANGE,
        SF2Gen.INITIAL_ATTENUATION,
        SF2Gen.COARSE_TUNE,
        SF2Gen.FINE_TUNE,
        SF2Gen.SAMPLE_ID,
        SF2Gen.SAMPLE_MODES,
        SF2Gen.SCALE_TUNING,
        SF2Gen.OVERRIDING_ROOT_KEY,
    }
    unsupported = sorted({
        operator for operator, amount in [*p_gens, *i_gens]
        if operator not in supported_operators and (operator != 0 or amount != 0)
    })
    if unsupported:
        warnings.append(
            "Ignored SF2 generators with no BRBNK equivalent: "
            + ", ".join(str(operator) for operator in unsupported)
        )
    for tag in (b"pmod", b"imod"):
        mod_data = pdta.get(tag, b"")
        records = _records(mod_data, 10, tag.decode()) if mod_data else []
        if any(any(record) for record in records[:-1]):
            warnings.append(f"Ignored {tag.decode()} modulators; BRBNK has no SoundFont modulator model")
    if b"sm24" in sdta:
        warnings.append("Imported the 16-bit part of 24-bit SF2 sample data")
    programs: dict[int, tuple[str, list[_Zone]]] = {}
    for preset_index in range(max(0, len(presets) - 1)):
        preset = presets[preset_index]
        program = preset.bank * 128 + preset.preset
        if program > 255:
            raise ValueError(f"SF2 preset {preset.name!r} maps to unsupported program {program}")
        if program in programs:
            raise ValueError(f"SF2 defines program {program} more than once")
        preset_zones = _header_zones(presets, preset_index, p_bags, p_gens)
        preset_global, preset_local_zones = _split_global_zone(
            preset_zones,
            SF2Gen.INSTRUMENT,
            f"preset {preset.name!r}",
            warnings,
        )
        converted: list[_Zone] = []
        for preset_zone in preset_local_zones:
            instrument_refs = [amount & 0xFFFF for oper, amount in preset_zone if oper == SF2Gen.INSTRUMENT]
            instrument_index = instrument_refs[-1]
            if not 0 <= instrument_index < len(instruments) - 1:
                raise ValueError(f"Preset {preset.name!r} references invalid instrument {instrument_index}")
            instrument_zones = _header_zones(instruments, instrument_index, i_bags, i_gens)
            instrument_global, instrument_local_zones = _split_global_zone(
                instrument_zones,
                SF2Gen.SAMPLE_ID,
                f"instrument {instruments[instrument_index].name!r}",
                warnings,
            )
            for instrument_zone in instrument_local_zones:
                zone = _zone_from_generators(
                    _resolve_generators(
                        preset_global,
                        preset_zone,
                        instrument_global,
                        instrument_zone,
                    ),
                    warnings,
                )
                if zone is not None:
                    if not 0 <= zone.sample_index < len(samples) - 1:
                        raise ValueError(
                            f"Instrument {instruments[instrument_index].name!r} references invalid sample "
                            f"{zone.sample_index}"
                        )
                    converted.append(zone)
        programs[program] = (preset.name, converted)

    # Resolve sample-header pitch metadata before recognizing conventional
    # overlapping L/R stereo zones. Differences in pitch correction or root
    # key make such zones genuinely incompatible layers.
    for _, zones in programs.values():
        for zone in zones:
            sample = samples[zone.sample_index]
            if zone.original_key is None:
                if 0 <= sample.original_pitch <= 127:
                    zone.original_key = sample.original_pitch
                else:
                    zone.original_key = 60
                    _warn_once(
                        warnings,
                        f"Sample {sample.name!r} has unpitched/invalid originalPitch="
                        f"{sample.original_pitch}; used MIDI key 60",
                    )
            if sample.pitch_correction:
                zone.pitch = max(
                    1 / 16,
                    min(16.0, zone.pitch * (2.0 ** (sample.pitch_correction / 1200.0))),
                )
    for program, (name, zones) in list(programs.items()):
        programs[program] = (name, _collapse_stereo_zones(zones, samples, warnings))

    referenced = sorted({
        (zone.sample_index, zone.looped)
        for _, zones in programs.values()
        for zone in zones
    })
    if referenced:
        warnings.append(
            "SF2 PCM samples were encoded as Wii DSP-ADPCM; this audio conversion is lossy"
        )
    wave_archive = Brwar.new()
    sample_map: dict[tuple[int, bool], int] = {}
    for sample_index, is_looped in referenced:
        sample = samples[sample_index]
        if not 400 <= sample.sample_rate <= 192000:
            raise ValueError(f"SF2 sample {sample.name!r} has invalid rate {sample.sample_rate}")
        pcm = _sample_pcm(sample_index, samples, smpl, warnings)
        loop_start = -1
        if is_looped:
            if not sample.start <= sample.loop_start < sample.loop_end <= sample.end:
                raise ValueError(f"Looping SF2 sample {sample.name!r} has invalid loop points")
            loop_start = sample.loop_start - sample.start
            loop_end = sample.loop_end - sample.start
        else:
            loop_end = -1
        wave_index = wave_archive.add(Brwav.from_pcm(
            pcm,
            sample.sample_rate,
            encoding=AudioCodec.ADPCM,
            loop_start=loop_start,
            loop_end=loop_end,
        ))
        sample_map[(sample_index, is_looped)] = wave_index

    highest_program = max(programs, default=-1)
    bank = BrbnkData(instruments=[
        Instrument(program=i, root_region=InstrumentRegion.null())
        for i in range(highest_program + 1)
    ])
    for program, (name, zones) in programs.items():
        bank.instruments[program] = Instrument(
            program=program,
            name=name,
            root_region=_partition(zones, sample_map),
        )
    return SF2ImportResult(bank, wave_archive, source_name, warnings)
