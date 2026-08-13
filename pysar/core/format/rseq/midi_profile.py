from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote

from pysar.core.format.rseq.midi import (
    MidiEvent,
    MidiEventType,
    MidiFile,
    MidiMetaType,
    MidiTrack,
    midi_to_brseq,
)
from pysar.core.format.rseq.mml import DEFAULT_TIMEBASE, MML, MMLEX, is_note
from pysar.core.format.rseq.reader import BrseqReader
from pysar.core.format.rseq.writer import BrseqWriter
from pysar.core.model.brseq import BrseqData, Command, Label, Prefix


ANNOTATION_PREFIX = "@pysar/1"
ANNOTATION_TRACK_NAME = "@PYSAR Nintendo"
PROFILE_TRACK_NAME = ANNOTATION_TRACK_NAME  # Backwards-compatible public name.
_OLD_PROFILE_TRACK_NAME = "@PYSAR Source"
_OLD_PROFILE_MAGIC = b"PYSAR\x00NW4R\x00"
_STRUCTURAL_KINDS = {
    "call": MML.CALL,
    "jump": MML.JUMP,
    "open-track": MML.OPEN_TRACK,
    "loop-start": MML.LOOP_START,
    "loop-end": MML.LOOP_END,
    "ret": MML.RET,
    "fin": MML.FIN,
}


@dataclass(frozen=True)
class ProfileDiagnostic:
    severity: str
    code: str
    message: str
    location: str | None = None


@dataclass
class ProfileImportResult:
    data: BrseqData
    diagnostics: list[ProfileDiagnostic] = field(default_factory=list)
    guarantee: str = "performance"
    profile: "NintendoMidiProfile | None" = None
    entry_label: str | None = None
    entry_offset: int | None = None

    @property
    def brseq(self):
        from pysar.core.format.rseq.brseq import Brseq
        return Brseq(self.data)


@dataclass
class AnnotationCommand:
    id: str
    order: int
    opcode: int
    args: list[int] = field(default_factory=list)
    prefixes: list[tuple[int, list[int]]] = field(default_factory=list)
    extended: bool = False
    target_argument: int | None = None
    target: str | None = None
    raw: bytes | None = None
    tick: int = 0


@dataclass
class AnnotationLabel:
    id: str
    name: str
    before: str | None
    index: int
    tick: int = 0


@dataclass
class AnnotationBinding:
    command: str
    order: int
    midi_track: int
    channel: int
    ordinal: int
    tick: int
    key: int
    velocity: int
    duration: int
    source_opcode: int | None = None
    source_args: list[int] = field(default_factory=list)
    source_prefixes: list[tuple[int, list[int]]] = field(default_factory=list)
    source_extended: bool = False


@dataclass
class _MidiNote:
    ordinal: int
    channel: int
    tick: int
    key: int
    velocity: int
    duration: int
    source_id: str | None = None


class NintendoMidiProfile:
    """Parsed PNMP/1 annotations.

    The historical class name is retained as the public API, but there is no
    JSON manifest: each record is an ordinary readable MIDI Text/Marker event.
    """

    def __init__(
            self,
            *,
            brseq_version: int = 0x0100,
            timebase: int = DEFAULT_TIMEBASE,
            entry: str | None = None,
            commands: Iterable[AnnotationCommand] = (),
            labels: Iterable[AnnotationLabel] = (),
            bindings: Iterable[AnnotationBinding] = (),
            midi: MidiFile | None = None,
    ):
        self.brseq_version = int(brseq_version)
        self.timebase = max(1, int(timebase))
        self.entry = entry
        self.commands = list(commands)
        self.labels = list(labels)
        self.bindings = list(bindings)
        self._midi = midi
        self.validate()

    @classmethod
    def from_brseq(
            cls,
            brseq: BrseqData,
            midi: MidiFile,
            *,
            start_label: str | None = None,
            start_offset: int | None = None,
    ) -> "NintendoMidiProfile":
        ordered = BrseqWriter()._ordered_commands(brseq)
        ids = {id(command): f"c{index:06d}" for index, command in enumerate(ordered)}
        offset_ids: dict[int, str] = {}
        for command in ordered:
            offset_ids.setdefault(int(command.offset), ids[id(command)])

        label_names_by_offset: dict[int, str] = {}
        labels = []
        for index, label in enumerate(brseq.labels):
            label_names_by_offset.setdefault(int(label.offset), label.name)
            labels.append(AnnotationLabel(
                id=f"l{index:04d}",
                name=label.name,
                before=offset_ids.get(int(label.offset)),
                index=index,
                tick=int(getattr(midi, "source_command_ticks", {}).get(int(label.offset), 0)),
            ))

        commands = []
        for order, command in enumerate(ordered):
            target_argument = None
            target = None
            mml = command.get_mml()
            if mml in (MML.JUMP, MML.CALL) and command.args:
                target_argument = 0
            elif mml == MML.OPEN_TRACK and len(command.args) >= 2:
                target_argument = 1
            if target_argument is not None:
                target_offset = int(command.args[target_argument])
                target = label_names_by_offset.get(target_offset) or offset_ids.get(target_offset)
            commands.append(AnnotationCommand(
                id=ids[id(command)],
                order=order,
                opcode=int(command.opcode),
                args=[int(value) for value in command.args],
                prefixes=[
                    (int(prefix.type), [int(value) for value in prefix.args])
                    for prefix in command.prefixes
                ],
                extended=bool(command.is_extended),
                target_argument=target_argument,
                target=target,
                raw=(command.raw_bytes if _requires_raw_annotation(command) else None),
                tick=int(getattr(midi, "source_command_ticks", {}).get(int(command.offset), 0)),
            ))

        entry = None
        if start_label is not None:
            entry = start_label
        elif start_offset is not None:
            entry = label_names_by_offset.get(int(start_offset)) or offset_ids.get(int(start_offset))
        elif labels:
            entry = labels[0].name

        for track in midi.tracks:
            for event in track.events:
                source_offset = getattr(event, "source_offset", None)
                if source_offset is not None:
                    event.source_id = offset_ids.get(int(source_offset))

        timebase = DEFAULT_TIMEBASE
        for command in ordered:
            if command.get_mml() == MML.TIMEBASE and command.args:
                timebase = int(command.args[0])
                break
        return cls(
            brseq_version=brseq.version,
            timebase=timebase,
            entry=entry,
            commands=commands,
            labels=labels,
            bindings=_make_performance_bindings(midi, commands),
            midi=midi,
        )

    @classmethod
    def from_midi(cls, midi: MidiFile) -> "NintendoMidiProfile | None":
        sequence = None
        commands: list[AnnotationCommand] = []
        labels: list[AnnotationLabel] = []
        bindings: list[AnnotationBinding] = []
        auto_order = 0

        for track_index, track in enumerate(midi.tracks):
            tick = 0
            for event in track.events:
                tick += int(event.delta_time)
                text = _annotation_text(event)
                if text is None:
                    continue
                kind, fields = _parse_annotation(text)
                if kind == "sequence":
                    sequence = fields
                elif kind == "label":
                    labels.append(AnnotationLabel(
                        id=fields.get("id", f"l{len(labels):04d}"),
                        name=_required(fields, "name", "label"),
                        before=_none(fields.get("before")),
                        index=_integer(fields.get("index"), len(labels)),
                        tick=tick,
                    ))
                elif kind == "command":
                    command = _command_from_fields(fields, auto_order, tick)
                    commands.append(command)
                    auto_order = max(auto_order + 1, command.order + 1)
                elif kind in _STRUCTURAL_KINDS:
                    command = _structural_command_from_fields(
                        kind, fields, auto_order, tick,
                    )
                    commands.append(command)
                    auto_order = max(auto_order + 1, command.order + 1)
                elif kind == "bind":
                    bindings.append(AnnotationBinding(
                        command=fields.get("command", fields.get("id", f"c{auto_order:06d}")),
                        order=_integer(fields.get("order"), auto_order),
                        midi_track=track_index,
                        channel=_integer(fields.get("channel"), 0),
                        ordinal=_integer(fields.get("ordinal"), -1),
                        tick=_integer(fields.get("tick"), tick),
                        key=_integer(fields.get("key"), -1),
                        velocity=_integer(fields.get("velocity"), -1),
                        duration=_integer(fields.get("duration"), -1),
                        source_opcode=(
                            _integer(fields["source-opcode"])
                            if "source-opcode" in fields else None
                        ),
                        source_args=_parse_ints(fields.get("source-args", "-")),
                        source_prefixes=_parse_prefixes(fields.get("source-prefixes", "")),
                        source_extended=_integer(fields.get("source-extended"), 0) != 0,
                    ))
                    auto_order += 1

        if sequence is None and not commands and not bindings and not labels:
            return None
        sequence = sequence or {}
        return cls(
            brseq_version=_integer(sequence.get("brseq-version"), 0x0100),
            timebase=_integer(sequence.get("timebase"), DEFAULT_TIMEBASE),
            entry=_none(sequence.get("entry")),
            commands=commands,
            labels=labels,
            bindings=bindings,
            midi=midi,
        )

    @classmethod
    def import_file(
            cls,
            path: str | Path,
            *,
            timebase: int = DEFAULT_TIMEBASE,
            combine_tracks: bool = False,
            merge_policy: str = "safe",
    ) -> ProfileImportResult:
        return cls.import_midi(
            MidiFile.from_file(path),
            timebase=timebase,
            combine_tracks=combine_tracks,
            merge_policy=merge_policy,
        )

    @classmethod
    def import_midi(
            cls,
            midi: MidiFile,
            *,
            profile: "NintendoMidiProfile | None" = None,
            timebase: int = DEFAULT_TIMEBASE,
            combine_tracks: bool = False,
            merge_policy: str = "safe",
            diagnostics: list[ProfileDiagnostic] | None = None,
    ) -> ProfileImportResult:
        messages = list(diagnostics or ())
        if merge_policy not in {"safe", "strict", "performance"}:
            raise ValueError(f"Unsupported PNMP merge policy '{merge_policy}'")
        if merge_policy == "performance":
            profile = None
        elif profile is None:
            profile = cls.from_midi(midi)
        if profile is None:
            return ProfileImportResult(
                midi_to_brseq(midi, timebase, combine_tracks), messages, "performance",
            )

        data = profile.to_brseq_data(midi, messages)
        conflicts = [item for item in messages if item.severity == "error"]
        if conflicts and merge_policy == "strict":
            raise ValueError("PNMP import conflicts: " + "; ".join(item.message for item in conflicts))
        data = _normalize_brseq(data)
        guarantee = "structural" if profile.commands else "annotated"
        entry_label, entry_offset = profile.resolve_entry(data)
        return ProfileImportResult(
            data, messages, guarantee, profile, entry_label, entry_offset,
        )

    def validate(self) -> None:
        ids = [command.id for command in self.commands]
        if len(ids) != len(set(ids)):
            raise ValueError("PNMP command IDs must be unique")
        orders = [command.order for command in self.commands]
        if len(orders) != len(set(orders)):
            raise ValueError("PNMP command order values must be unique")
        known = set(ids) | {binding.command for binding in self.bindings}
        label_names = {label.name for label in self.labels}
        for command in self.commands:
            if not 0 <= command.opcode <= 255:
                raise ValueError(f"PNMP command '{command.id}' has an invalid opcode")
            if command.target is not None and command.target not in known | label_names:
                raise ValueError(f"PNMP command '{command.id}' has unknown target '{command.target}'")
        for label in self.labels:
            if label.before is not None and label.before not in known:
                raise ValueError(f"PNMP label '{label.name}' has unknown position '{label.before}'")

    def resolve_entry(self, data: BrseqData) -> tuple[str | None, int | None]:
        """Resolve the annotated selected cue in a rebuilt command stream."""
        if self.entry is None:
            return None, None
        label = next((item for item in data.labels if item.name == self.entry), None)
        if label is not None:
            return label.name, int(label.offset)

        orders = {command.id: command.order for command in self.commands}
        for binding in self.bindings:
            orders.setdefault(binding.command, binding.order)
        target_order = orders.get(self.entry)
        if target_order is None:
            return None, None
        ordered_ids = [
            command_id for command_id, _order in sorted(
                orders.items(), key=lambda item: item[1],
            )
        ]
        try:
            index = ordered_ids.index(self.entry)
        except ValueError:
            return None, None
        if not 0 <= index < len(data.command_stream):
            return None, None
        return None, int(data.command_stream[index].offset)

    def embed(self, midi: MidiFile) -> None:
        """Write individual readable Text/Marker annotations into *midi*."""
        _remove_existing_annotations(midi)
        metadata: list[tuple[int, MidiEvent]] = []
        metadata.append((0, _text_event(
            MidiMetaType.TRACK_NAME, ANNOTATION_TRACK_NAME,
        )))
        sequence = (
            f"{ANNOTATION_PREFIX} sequence "
            f"brseq-version=0x{self.brseq_version:04X} timebase={self.timebase} "
            f"entry={_encode(self.entry or '-')}"
        )
        metadata.append((0, _text_event(MidiMetaType.TEXT, sequence)))

        for label in sorted(self.labels, key=lambda value: value.index):
            line = (
                f"{ANNOTATION_PREFIX} label id={_encode(label.id)} "
                f"name={_encode(label.name)} before={_encode(label.before or '-')} "
                f"index={label.index}"
            )
            metadata.append((label.tick, _text_event(MidiMetaType.MARKER, line)))

        marker_ops = {
            int(MML.CALL), int(MML.JUMP), int(MML.OPEN_TRACK),
            int(MML.LOOP_START), int(MML.LOOP_END), int(MML.RET), int(MML.FIN),
        }
        bound_commands = {binding.command for binding in self.bindings}
        for command in sorted(self.commands, key=lambda value: value.order):
            # A performed source note is represented by its normal MIDI note
            # plus bind event. Only unreachable/unrendered notes need their own
            # annotation, because there is no standard MIDI event to carry them.
            if (
                    is_note(command.opcode)
                    and not command.extended
                    and command.id in bound_commands
            ):
                continue
            line = _format_command_annotation(command)
            meta_type = MidiMetaType.MARKER if command.opcode in marker_ops else MidiMetaType.TEXT
            metadata.append((command.tick, _text_event(meta_type, line)))

        metadata.append((max((tick for tick, _event in metadata), default=0), MidiEvent(
            status=MidiEventType.META,
            data=bytes([MidiMetaType.END_OF_TRACK]),
        )))
        annotation_track = MidiTrack(name=ANNOTATION_TRACK_NAME)
        annotation_track.events = _abs_to_delta(metadata)
        midi.tracks.append(annotation_track)

        bindings_by_track: dict[int, list[AnnotationBinding]] = {}
        for binding in self.bindings:
            bindings_by_track.setdefault(binding.midi_track, []).append(binding)
        for track_index, bindings in bindings_by_track.items():
            if not 0 <= track_index < len(midi.tracks):
                continue
            track = midi.tracks[track_index]
            absolute: list[tuple[int, MidiEvent]] = []
            tick = 0
            for event in track.events:
                tick += int(event.delta_time)
                absolute.append((tick, event))
            for binding in bindings:
                line = (
                    f"{ANNOTATION_PREFIX} bind command={_encode(binding.command)} "
                    f"order={binding.order} channel={binding.channel} ordinal={binding.ordinal} "
                    f"tick={binding.tick} key={binding.key} velocity={binding.velocity} "
                    f"duration={binding.duration}"
                )
                if binding.source_opcode is not None:
                    line += (
                        f" source-opcode=0x{binding.source_opcode:02X}"
                        f" source-args={_encode(_format_ints(binding.source_args))}"
                    )
                    if binding.source_prefixes:
                        prefixes = "|".join(
                            f"{kind}:{_format_ints(args)}"
                            for kind, args in binding.source_prefixes
                        )
                        line += f" source-prefixes={_encode(prefixes)}"
                    if binding.source_extended:
                        line += " source-extended=1"
                absolute.append((binding.tick, _text_event(MidiMetaType.TEXT, line)))
            track.events = _abs_to_delta(absolute)

    def to_brseq_data(
            self,
            midi: MidiFile | None = None,
            diagnostics: list[ProfileDiagnostic] | None = None,
    ) -> BrseqData:
        midi = midi or self._midi
        messages = diagnostics if diagnostics is not None else []
        command_records = {command.id: command for command in self.commands}
        notes = _collect_midi_notes(midi) if midi is not None else {}

        requested: dict[str, list[tuple[int, int, int]]] = {}
        for binding in self.bindings:
            current = notes.get((binding.midi_track, binding.channel), [])
            ordinal = binding.ordinal
            if ordinal < 0:
                ordinal = next((
                    note.ordinal for note in current
                    if note.tick == binding.tick and (binding.key < 0 or note.key == binding.key)
                ), -1)
            if ordinal < 0 or ordinal >= len(current):
                messages.append(ProfileDiagnostic(
                    "error", "missing-bound-event",
                    f"Annotated note for command '{binding.command}' is missing.",
                    binding.command,
                ))
                continue
            note = current[ordinal]
            duration = max(1, int(round(
                note.duration * self.timebase / max(1, midi.ticks_per_beat)
            )))
            record = command_records.get(binding.command)
            if record is None:
                record = AnnotationCommand(
                    id=binding.command,
                    order=binding.order,
                    opcode=(
                        binding.source_opcode
                        if binding.source_opcode is not None else note.key
                    ),
                    args=(
                        list(binding.source_args)
                        if binding.source_args else [note.velocity, duration]
                    ),
                    prefixes=[(kind, list(args)) for kind, args in binding.source_prefixes],
                    extended=binding.source_extended,
                    tick=binding.tick,
                )
                command_records[record.id] = record
            if is_note(record.opcode) and not record.extended:
                source_velocity = record.args[0] if len(record.args) >= 1 else note.velocity
                source_duration = record.args[1] if len(record.args) >= 2 else duration
                base_key = binding.key if binding.key >= 0 else note.key
                base_velocity = binding.velocity if binding.velocity >= 0 else note.velocity
                base_duration = binding.duration if binding.duration >= 0 else note.duration
                source_key = record.opcode
                key = source_key + note.key - base_key
                velocity = source_velocity + note.velocity - base_velocity
                length = max(1, source_duration + int(round(
                    (note.duration - base_duration)
                    * self.timebase / max(1, midi.ticks_per_beat)
                )))
                requested.setdefault(record.id, []).append((key, velocity, length))

        for command_id, values in requested.items():
            record = command_records[command_id]
            if len(set(values)) != 1:
                messages.append(ProfileDiagnostic(
                    "error", "shared-command-divergence",
                    f"Repeated source command '{command_id}' was edited differently; its annotation was retained.",
                    command_id,
                ))
                continue
            key, velocity, duration = values[0]
            record.opcode = max(0, min(127, key))
            if len(record.args) >= 2:
                record.args[0] = max(0, min(127, velocity))
                record.args[1] = duration

        ordered = sorted(command_records.values(), key=lambda value: value.order)
        if not ordered:
            if midi is None:
                raise ValueError("PNMP annotations contain no commands or bound notes")
            messages.append(ProfileDiagnostic(
                "warning", "annotation-fallback",
                "No complete annotated command stream was found; standard MIDI conversion was used.",
            ))
            return midi_to_brseq(midi, self.timebase, False)

        command_by_id: dict[str, Command] = {}
        for record in ordered:
            command = Command(
                opcode=record.opcode,
                args=list(record.args),
                prefixes=[Prefix(type=MML(kind), args=list(args)) for kind, args in record.prefixes],
            )
            command._is_extended = record.extended
            command.raw_bytes = record.raw
            command_by_id[record.id] = command

        writer = BrseqWriter()
        offset = 0
        for record in ordered:
            command_by_id[record.id].offset = offset
            offset += writer._command_size(command_by_id[record.id])
        offsets = {record.id: command_by_id[record.id].offset for record in ordered}
        label_offsets = {
            label.name: offsets[label.before] if label.before is not None else offset
            for label in self.labels
        }
        for record in ordered:
            if record.target_argument is None or record.target is None:
                continue
            command = command_by_id[record.id]
            target_offset = label_offsets.get(record.target, offsets.get(record.target))
            if target_offset is None:
                raise ValueError(f"Unknown annotation target '{record.target}'")
            if record.target_argument >= len(command.args):
                raise ValueError(f"Target argument is out of range for '{record.id}'")
            command.args[record.target_argument] = target_offset

        labels = [
            Label(
                name=label.name,
                offset=offsets[label.before] if label.before is not None else offset,
            )
            for label in sorted(self.labels, key=lambda value: value.index)
        ]
        stream = [command_by_id[record.id] for record in ordered]
        tracks = BrseqReader()._build_tracks(labels, stream, offset)
        return BrseqData(
            version=self.brseq_version,
            labels=labels,
            tracks=tracks,
            command_stream=stream,
        )


def annotate_midi(
        midi: MidiFile,
        brseq: BrseqData,
        *,
        start_label: str | None = None,
        start_offset: int | None = None,
) -> NintendoMidiProfile:
    annotations = NintendoMidiProfile.from_brseq(
        brseq, midi, start_label=start_label, start_offset=start_offset,
    )
    annotations.embed(midi)
    return annotations


def _format_command_annotation(command: AnnotationCommand) -> str:
    op = _opcode_name(command.opcode, command.extended)
    common = (
        f"{ANNOTATION_PREFIX} {op.replace('_', '-')} "
        f"id={_encode(command.id)} order={command.order}"
    )
    if not command.extended and command.opcode == MML.CALL:
        return _append_command_extras(common + f" target={_encode(command.target or '-')}", command)
    if not command.extended and command.opcode == MML.JUMP:
        return _append_command_extras(common + f" target={_encode(command.target or '-')}", command)
    if not command.extended and command.opcode == MML.OPEN_TRACK:
        track = command.args[0] if command.args else 0
        return _append_command_extras(
            common + f" track={track} target={_encode(command.target or '-')}", command,
        )
    if not command.extended and command.opcode == MML.LOOP_START:
        count = command.args[0] if command.args else 0
        return _append_command_extras(common + f" count={count}", command)
    if not command.extended and command.opcode in (MML.LOOP_END, MML.RET, MML.FIN):
        return _append_command_extras(common, command)

    parts = [
        ANNOTATION_PREFIX,
        "command",
        f"id={_encode(command.id)}",
        f"order={command.order}",
        f"op={_encode(op)}",
        f"opcode=0x{command.opcode:02X}",
        f"args={_encode(_format_ints(command.args))}",
    ]
    if command.extended:
        parts.append("extended=1")
    if command.prefixes:
        encoded = "|".join(
            f"{kind}:{_format_ints(args)}" for kind, args in command.prefixes
        )
        parts.append(f"prefixes={_encode(encoded)}")
    if command.target is not None:
        parts.append(f"target={_encode(command.target)}")
        parts.append(f"target-arg={command.target_argument}")
    if command.raw is not None:
        parts.append(f"raw={command.raw.hex().upper()}")
    return " ".join(parts)


def _append_command_extras(line: str, command: AnnotationCommand) -> str:
    if command.prefixes:
        encoded = "|".join(
            f"{kind}:{_format_ints(args)}" for kind, args in command.prefixes
        )
        line += f" prefixes={_encode(encoded)}"
    if command.raw is not None:
        line += f" raw={command.raw.hex().upper()}"
    return line


def _structural_command_from_fields(
        kind: str,
        fields: dict[str, str],
        default_order: int,
        tick: int,
) -> AnnotationCommand:
    opcode = int(_STRUCTURAL_KINDS[kind])
    target = None
    target_argument = None
    if kind in ("call", "jump"):
        args = [0]
        target = _required(fields, "target", kind)
        target_argument = 0
    elif kind == "open-track":
        args = [_integer(fields.get("track"), 0), 0]
        target = _required(fields, "target", kind)
        target_argument = 1
    elif kind == "loop-start":
        args = [_integer(fields.get("count"), 0)]
    else:
        args = []
    return AnnotationCommand(
        id=fields.get("id", f"c{default_order:06d}"),
        order=_integer(fields.get("order"), default_order),
        opcode=opcode,
        args=args,
        prefixes=_parse_prefixes(fields.get("prefixes", "")),
        target_argument=target_argument,
        target=target,
        raw=bytes.fromhex(fields["raw"]) if fields.get("raw") else None,
        tick=tick,
    )


def _command_from_fields(fields: dict[str, str], default_order: int, tick: int) -> AnnotationCommand:
    extended = _integer(fields.get("extended"), 0) != 0
    if "opcode" in fields:
        opcode = _integer(fields["opcode"])
    else:
        opcode, extended = _parse_opcode(_required(fields, "op", "command"), extended)
    return AnnotationCommand(
        id=fields.get("id", f"c{default_order:06d}"),
        order=_integer(fields.get("order"), default_order),
        opcode=opcode,
        args=_parse_ints(fields.get("args", "-")),
        prefixes=_parse_prefixes(fields.get("prefixes", "")),
        extended=extended,
        target_argument=(
            _integer(fields["target-arg"])
            if "target-arg" in fields
            else _default_target_argument(opcode)
        ),
        target=_none(fields.get("target")),
        raw=bytes.fromhex(fields["raw"]) if fields.get("raw") else None,
        tick=tick,
    )


def _opcode_name(opcode: int, extended: bool) -> str:
    if is_note(opcode) and not extended:
        return "note"
    enum = MMLEX if extended else MML
    try:
        return enum(opcode).name.lower()
    except ValueError:
        return f"0x{opcode:02X}"


def _requires_raw_annotation(command: Command) -> bool:
    if is_note(command.opcode) and not command.is_extended:
        return False
    try:
        (MMLEX if command.is_extended else MML)(command.opcode)
        return False
    except ValueError:
        return command.raw_bytes is not None


def _parse_opcode(value: str, extended: bool) -> tuple[int, bool]:
    name = value.lower().replace("-", "_")
    if name == "note":
        raise ValueError("A note command needs opcode=<MIDI key> or a bind annotation")
    if name.startswith("0x"):
        return int(name, 16), extended
    try:
        return int(MMLEX[name.upper()] if extended else MML[name.upper()]), extended
    except KeyError as exc:
        raise ValueError(f"Unknown Nintendo command '{value}'") from exc


def _default_target_argument(opcode: int) -> int | None:
    if opcode in (int(MML.JUMP), int(MML.CALL)):
        return 0
    if opcode == int(MML.OPEN_TRACK):
        return 1
    return None


def _parse_annotation(text: str) -> tuple[str, dict[str, str]]:
    tail = text[len(ANNOTATION_PREFIX):].strip()
    pieces = tail.split()
    if not pieces:
        raise ValueError("Empty @pysar/1 annotation")
    fields: dict[str, str] = {}
    for token in pieces[1:]:
        if "=" not in token:
            raise ValueError(f"Invalid @pysar/1 annotation field '{token}'")
        key, value = token.split("=", 1)
        fields[key.lower()] = unquote(value)
    return pieces[0].lower(), fields


def _annotation_text(event: MidiEvent) -> str | None:
    if event.status != MidiEventType.META or not event.data:
        return None
    if event.data[0] not in (MidiMetaType.TEXT, MidiMetaType.MARKER):
        return None
    text = event.data[1:].decode("utf-8", errors="replace").strip()
    return text if text.startswith(ANNOTATION_PREFIX + " ") else None


def _remove_existing_annotations(midi: MidiFile) -> None:
    kept = []
    for track in midi.tracks:
        if track.name in (ANNOTATION_TRACK_NAME, _OLD_PROFILE_TRACK_NAME):
            continue
        track.events = [
            event for event in track.events
            if _annotation_text(event) is None and not (
                event.status == MidiEventType.META
                and len(event.data) > 1
                and event.data[0] == 0x7F
                and event.data[1:].startswith(_OLD_PROFILE_MAGIC)
            )
        ]
        kept.append(track)
    midi.tracks = kept


def _collect_midi_notes(midi: MidiFile | None) -> dict[tuple[int, int], list[_MidiNote]]:
    result: dict[tuple[int, int], list[_MidiNote]] = {}
    if midi is None:
        return result
    for track_index, track in enumerate(midi.tracks):
        tick = 0
        active: dict[tuple[int, int], list[_MidiNote]] = {}
        ordinals: dict[int, int] = {}
        for event in track.events:
            tick += int(event.delta_time)
            if event.is_note_on():
                channel = event.channel
                ordinal = ordinals.get(channel, 0)
                ordinals[channel] = ordinal + 1
                note = _MidiNote(
                    ordinal, channel, tick, event.note, event.velocity, 1,
                    getattr(event, "source_id", None),
                )
                result.setdefault((track_index, channel), []).append(note)
                active.setdefault((channel, event.note), []).append(note)
            elif event.is_note_off():
                pending = active.get((event.channel, event.note), [])
                if pending:
                    note = pending.pop(0)
                    note.duration = max(1, tick - note.tick)
    return result


def _make_performance_bindings(
        midi: MidiFile,
        commands: Iterable[AnnotationCommand],
) -> list[AnnotationBinding]:
    command_records = {command.id: command for command in commands}
    bindings = []
    for (track_index, channel), notes in sorted(_collect_midi_notes(midi).items()):
        for note in notes:
            if note.source_id is None:
                continue
            source = command_records[note.source_id]
            bindings.append(AnnotationBinding(
                command=note.source_id,
                order=source.order,
                midi_track=track_index,
                channel=channel,
                ordinal=note.ordinal,
                tick=note.tick,
                key=note.key,
                velocity=note.velocity,
                duration=note.duration,
                source_opcode=source.opcode,
                source_args=list(source.args),
                source_prefixes=[(kind, list(args)) for kind, args in source.prefixes],
                source_extended=source.extended,
            ))
    return bindings


def _text_event(meta_type: int, text: str) -> MidiEvent:
    return MidiEvent(
        status=MidiEventType.META,
        data=bytes([int(meta_type)]) + text.encode("utf-8"),
    )


def _abs_to_delta(events: list[tuple[int, MidiEvent]]) -> list[MidiEvent]:
    events.sort(key=lambda item: item[0])
    result = []
    previous = 0
    for tick, event in events:
        event.delta_time = max(0, int(tick) - previous)
        previous = int(tick)
        result.append(event)
    return result


def _encode(value: str) -> str:
    return quote(str(value), safe="._-:")


def _none(value: str | None) -> str | None:
    return None if value in (None, "", "-") else value


def _required(fields: dict[str, str], key: str, kind: str) -> str:
    value = fields.get(key)
    if value is None or value == "":
        raise ValueError(f"@pysar/1 {kind} requires {key}=...")
    return value


def _integer(value: str | int | None, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError("Missing integer annotation value")
        return default
    return int(value, 0) if isinstance(value, str) else int(value)


def _format_ints(values: Iterable[int]) -> str:
    values = list(values)
    return ",".join(str(int(value)) for value in values) if values else "-"


def _parse_ints(value: str) -> list[int]:
    if value in ("", "-"):
        return []
    return [_integer(item) for item in value.split(",")]


def _parse_prefixes(value: str) -> list[tuple[int, list[int]]]:
    if not value:
        return []
    output = []
    for item in value.split("|"):
        kind, separator, args = item.partition(":")
        output.append((_integer(kind), _parse_ints(args if separator else "-")))
    return output


def _normalize_brseq(data: BrseqData) -> BrseqData:
    output = BytesIO()
    BrseqWriter().write(data, output)
    return BrseqReader().read(BytesIO(output.getvalue()))


NintendoMidiAnnotations = NintendoMidiProfile
AnnotationDiagnostic = ProfileDiagnostic
AnnotationImportResult = ProfileImportResult
