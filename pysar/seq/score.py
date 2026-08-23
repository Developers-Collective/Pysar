"""Sheet-music conversion helpers for rendered sequence previews."""

from __future__ import annotations

from copy import deepcopy
from statistics import median
from typing import TypedDict
from xml.etree import ElementTree


class TempoMapPoint(TypedDict):
    quarter: float
    ms: float
    microsecondsPerQuarter: int


class ScoreTimePoint(TypedDict):
    scoreQuarter: float
    midiQuarter: float


def midi_bytes_to_musicxml_with_timing(
    midi_data: bytes,
    *,
    title: str | None = None,
) -> tuple[str, list[ScoreTimePoint], list[float], list[float]]:
    """Transcribe MIDI while retaining anchors to its unquantized timing."""
    try:
        from music21 import converter, metadata
        from music21.musicxml.m21ToXml import GeneralObjectExporter
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Sheet-music support is unavailable because music21 is not installed"
        ) from exc

    from pysar.core.format.rseq.midi import MidiFile

    score = converter.parse(midi_data, format="midi")
    if title:
        if score.metadata is None:
            score.metadata = metadata.Metadata()
        score.metadata.title = title

    midi = MidiFile.from_bytes(midi_data)
    time_map = _music21_score_time_map(score, max(1, int(midi.ticks_per_beat)))
    measure_starts, measure_durations = _music21_measure_timing(score)
    xml_data = GeneralObjectExporter(score).parse()
    return (
        _pad_musicxml_parts(xml_data).decode("utf-8"),
        time_map,
        measure_starts,
        measure_durations,
    )


def _music21_score_time_map(score, ticks_per_beat: int) -> list[ScoreTimePoint]:
    """Map music21's readable quantization back to exact MIDI onsets."""
    midi_quarters_by_score_quarter: dict[float, list[float]] = {}
    for part in score.parts:
        # Flatten once so every note already carries its part-relative offset.
        # Calling getOffsetInHierarchy for every note becomes very expensive
        # on the large multi-track sequences this view is intended for.
        for item in part.flatten().notes:
            if getattr(getattr(item, "tie", None), "type", None) in {
                "stop", "continue",
            }:
                continue
            midi_tick = getattr(item.editorial, "midiTickStart", None)
            if midi_tick is None:
                continue
            score_quarter = float(item.offset)
            midi_quarters_by_score_quarter.setdefault(score_quarter, []).append(
                float(midi_tick) / max(1, int(ticks_per_beat))
            )

    midi_quarters_by_score_quarter.setdefault(0.0, []).append(0.0)
    result: list[ScoreTimePoint] = []
    previous_midi_quarter = 0.0
    for score_quarter, midi_quarters in sorted(midi_quarters_by_score_quarter.items()):
        midi_quarter = max(previous_midi_quarter, float(median(midi_quarters)))
        result.append({
            "scoreQuarter": score_quarter,
            "midiQuarter": midi_quarter,
        })
        previous_midi_quarter = midi_quarter
    return result


def _music21_measure_timing(score) -> tuple[list[float], list[float]]:
    from music21 import stream

    candidates: list[tuple[object, list]] = []
    for part in score.parts:
        measures = list(part.getElementsByClass(stream.Measure))
        if measures:
            candidates.append((part, measures))
    if not candidates:
        return [0.0], [max(0.0, float(score.highestTime))]

    part, measures = max(candidates, key=lambda item: len(item[1]))
    starts = [float(measure.offset) for measure in measures]
    durations = [
        max(0.0, starts[index + 1] - start)
        for index, start in enumerate(starts[:-1])
    ]
    durations.append(max(0.0, float(part.highestTime) - starts[-1]))
    return starts, durations


def _musicxml_measure_duration(measure: ElementTree.Element) -> int:
    cursor = 0
    furthest = 0
    for child in measure:
        if child.tag == "backup":
            cursor -= int(child.findtext("duration", "0"))
        elif child.tag == "forward":
            cursor += int(child.findtext("duration", "0"))
            furthest = max(furthest, cursor)
        elif child.tag == "note":
            duration = int(child.findtext("duration", "0"))
            if child.find("chord") is None:
                cursor += duration
                furthest = max(furthest, cursor)
    return max(0, furthest)


def _pad_musicxml_parts(xml_data: bytes) -> bytes:
    """Pad shorter parts so the browser engraves the complete ensemble."""
    root = ElementTree.fromstring(xml_data)
    parts = root.findall("part")
    if len(parts) < 2:
        return xml_data

    measures_by_part = [part.findall("measure") for part in parts]
    target_count = max((len(measures) for measures in measures_by_part), default=0)
    if target_count == 0 or all(
        len(measures) == target_count for measures in measures_by_part
    ):
        return xml_data

    reference = next(
        measures for measures in measures_by_part if len(measures) == target_count
    )
    reference_durations = [_musicxml_measure_duration(measure) for measure in reference]
    for part, measures in zip(parts, measures_by_part):
        for index in range(len(measures), target_count):
            template = reference[index]
            attributes = {
                key: value for key, value in template.attrib.items()
                if key in {"number", "implicit", "non-controlling", "width"}
            }
            attributes.setdefault("number", str(index + 1))
            measure = ElementTree.Element("measure", attributes)

            source_attributes = template.find("attributes")
            if source_attributes is not None:
                carried = ElementTree.Element("attributes")
                for tag in ("divisions", "key", "time", "transpose"):
                    element = source_attributes.find(tag)
                    if element is not None:
                        carried.append(deepcopy(element))
                if len(carried):
                    measure.append(carried)

            duration = max(1, reference_durations[index])
            rest_note = ElementTree.SubElement(measure, "note")
            ElementTree.SubElement(rest_note, "rest", {"measure": "yes"})
            ElementTree.SubElement(rest_note, "duration").text = str(duration)
            ElementTree.SubElement(rest_note, "voice").text = "1"
            ElementTree.SubElement(rest_note, "type").text = "whole"
            part.append(measure)

    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def sequence_events_to_midi_bytes(
    events: list[dict],
    *,
    timebase: int,
    ticks_per_beat: int = 480,
) -> bytes:
    """Encode the exact sequence-player path as an internal notation MIDI."""
    from pysar.core.format.rseq.midi import (
        MidiEvent,
        MidiEventType,
        MidiFile,
        MidiMetaType,
        MidiTrack,
    )

    ticks_per_beat = max(1, int(ticks_per_beat))
    scale = ticks_per_beat / max(1, int(timebase))
    last_source_tick = max((int(event.get("tick", 0)) for event in events), default=0)
    end_tick = max(0, int(round(last_source_tick * scale)))

    def to_delta(absolute_events: list[tuple[int, int, MidiEvent]]) -> list[MidiEvent]:
        absolute_events.sort(key=lambda item: (item[0], item[1]))
        previous_tick = 0
        output: list[MidiEvent] = []
        for tick, _, event in absolute_events:
            event.delta_time = max(0, int(tick) - previous_tick)
            previous_tick = int(tick)
            output.append(event)
        return output

    midi = MidiFile(format_type=1, ticks_per_beat=ticks_per_beat)
    conductor_events: list[tuple[int, int, MidiEvent]] = [
        (0, 0, MidiEvent(
            status=MidiEventType.META,
            data=bytes([MidiMetaType.TRACK_NAME]) + b"Conductor",
        )),
        (0, 1, MidiEvent(
            status=MidiEventType.META,
            data=bytes([MidiMetaType.TIME_SIGNATURE, 4, 2, 24, 8]),
        )),
    ]
    tempo_order = 2
    tempo_ticks: set[int] = set()
    for event in events:
        if event.get("type") != "tempo":
            continue
        midi_tick = max(0, int(round(int(event.get("tick", 0)) * scale)))
        bpm = max(1, int(event.get("tempo", 120) or 120))
        micros = max(1, int(round(60_000_000 / bpm)))
        conductor_events = [
            item for item in conductor_events
            if not (
                item[0] == midi_tick
                and item[2].status == MidiEventType.META
                and item[2].data[:1] == bytes([MidiMetaType.SET_TEMPO])
            )
        ]
        conductor_events.append((midi_tick, tempo_order, MidiEvent(
            status=MidiEventType.META,
            data=bytes([
                MidiMetaType.SET_TEMPO,
                (micros >> 16) & 0xFF,
                (micros >> 8) & 0xFF,
                micros & 0xFF,
            ]),
        )))
        tempo_order += 1
        tempo_ticks.add(midi_tick)
    if not tempo_ticks:
        conductor_events.append((0, tempo_order, MidiEvent(
            status=MidiEventType.META,
            data=bytes([MidiMetaType.SET_TEMPO, 0x07, 0xA1, 0x20]),
        )))
    conductor_events.append((end_tick, 1_000_000, MidiEvent(
        status=MidiEventType.META,
        data=bytes([MidiMetaType.END_OF_TRACK]),
    )))
    midi.tracks.append(MidiTrack(
        name="Conductor",
        events=to_delta(conductor_events),
    ))

    musical_types = {"note_on", "note_off", "note_change", "program_change"}
    events_by_track: dict[int, list[dict]] = {}
    for event in events:
        if event.get("track") is None or event.get("type") not in musical_types:
            continue
        track_index = max(0, int(event.get("track", 0)))
        events_by_track.setdefault(track_index, []).append(event)

    # MIDI channel 10 is percussion by convention. Nintendo track numbers do
    # not carry that meaning, so keep notation tracks on melodic channels.
    melodic_channels = tuple(range(9)) + tuple(range(10, 16))
    for position, (track_index, track_events) in enumerate(sorted(events_by_track.items())):
        channel = melodic_channels[position % len(melodic_channels)]
        name = f"Track {track_index + 1}"
        absolute_events: list[tuple[int, int, MidiEvent]] = [
            (0, 0, MidiEvent(
                status=MidiEventType.META,
                data=bytes([MidiMetaType.TRACK_NAME]) + name.encode("ascii"),
            )),
        ]
        last_program: int | None = None
        order = 1
        for event in track_events:
            midi_tick = max(0, int(round(int(event.get("tick", 0)) * scale)))
            event_type = str(event.get("type"))
            if event_type in {"note_on", "note_change", "program_change"}:
                program = max(0, min(127, int(event.get("program", 0))))
                if program != last_program:
                    absolute_events.append((midi_tick, order, MidiEvent(
                        status=MidiEventType.PROGRAM_CHANGE | channel,
                        data=bytes([program]),
                    )))
                    order += 1
                    last_program = program

            if event_type == "note_change":
                absolute_events.append((midi_tick, order, MidiEvent(
                    status=MidiEventType.NOTE_OFF | channel,
                    data=bytes([
                        max(0, min(127, int(event.get("old_note", 0)))), 0,
                    ]),
                )))
                order += 1
                event_type = "note_on"

            if event_type == "note_on":
                absolute_events.append((midi_tick, order, MidiEvent(
                    status=MidiEventType.NOTE_ON | channel,
                    data=bytes([
                        max(0, min(127, int(event.get("note", 0)))),
                        max(1, min(127, int(event.get("velocity", 127)))),
                    ]),
                )))
                order += 1
            elif event_type == "note_off":
                absolute_events.append((midi_tick, order, MidiEvent(
                    status=MidiEventType.NOTE_OFF | channel,
                    data=bytes([
                        max(0, min(127, int(event.get("note", 0)))), 0,
                    ]),
                )))
                order += 1

        absolute_events.append((end_tick, 1_000_000, MidiEvent(
            status=MidiEventType.META,
            data=bytes([MidiMetaType.END_OF_TRACK]),
        )))
        midi.tracks.append(MidiTrack(name=name, events=to_delta(absolute_events)))

    if len(midi.tracks) == 1:
        midi.tracks.append(MidiTrack(
            name="Track 1",
            events=to_delta([
                (0, 0, MidiEvent(
                    status=MidiEventType.META,
                    data=bytes([MidiMetaType.TRACK_NAME]) + b"Track 1",
                )),
                (end_tick, 1, MidiEvent(
                    status=MidiEventType.META,
                    data=bytes([MidiMetaType.END_OF_TRACK]),
                )),
            ]),
        ))

    return midi.to_bytes()


def midi_bytes_to_tempo_map(midi_data: bytes) -> list[TempoMapPoint]:
    """Map MIDI quarter-note coordinates to playback milliseconds."""
    from pysar.core.format.rseq.midi import MidiEventType, MidiFile, MidiMetaType

    midi = MidiFile.from_bytes(midi_data)
    tempo_by_tick: dict[int, int] = {0: 500_000}
    for track in midi.tracks:
        absolute_tick = 0
        for event in track.events:
            absolute_tick += int(event.delta_time)
            if (
                event.status == MidiEventType.META
                and len(event.data) >= 4
                and event.data[0] == MidiMetaType.SET_TEMPO
            ):
                micros = (
                    (int(event.data[1]) << 16)
                    | (int(event.data[2]) << 8)
                    | int(event.data[3])
                )
                if micros > 0:
                    tempo_by_tick[absolute_tick] = micros

    ticks_per_beat = max(1, int(midi.ticks_per_beat))
    previous_tick = 0
    current_micros = tempo_by_tick[0]
    elapsed_ms = 0.0
    tempo_map: list[TempoMapPoint] = []
    for tick, micros in sorted(tempo_by_tick.items()):
        elapsed_ms += (
            (tick - previous_tick) * current_micros / ticks_per_beat / 1000.0
        )
        tempo_map.append({
            "quarter": tick / ticks_per_beat,
            "ms": elapsed_ms,
            "microsecondsPerQuarter": micros,
        })
        previous_tick = tick
        current_micros = micros
    return tempo_map
