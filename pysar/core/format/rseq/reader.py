import struct
from typing import BinaryIO

from pysar.core import NW4RUnsupportedVersionError, NW4RInvalidFileError
from pysar.core.base import ReaderBase
from pysar.core.format.rseq.mml import (
    MML, MMLEX, ArgType, MML_ARG_SPEC, MMLEX_ARG_SPEC, PREFIXABLE_COMMANDS,
    is_note,
)
from pysar.core.model.brseq import BrseqData, Label, Track, Command, Prefix


class BrseqReader(ReaderBase):
    EXPECTED_MAGIC = b"RSEQ"
    SUPPORTED_VERSIONS = {0x0100}

    def read(self, data: BinaryIO) -> BrseqData:
        """Read a BRSEQ file from a binary stream."""
        base_offset = data.tell()

        # Read file header
        header = data.read(0x20)
        if len(header) < 0x20 or header[:4] != b"RSEQ":
            raise NW4RInvalidFileError("Not a valid BRSEQ file")

        (
            bom, version, file_size, header_size, n_sections,
            data_offset, data_size,
            label_offset, label_size,
        ) = struct.unpack(">HHIHH IIII", header[4:0x20])

        if version not in self.SUPPORTED_VERSIONS:
            raise NW4RUnsupportedVersionError(
                f"Unsupported BRSEQ version: 0x{version:04X}"
            )

        # Read LABL block (labels)
        labels = []
        if label_offset > 0 and label_size > 0:
            data.seek(base_offset + label_offset)
            labels = self._read_label_block(data)

        # Read DATA block
        data.seek(base_offset + data_offset)
        data_block_header = data.read(12)
        if data_block_header[:4] != b"DATA":
            raise NW4RInvalidFileError("DATA block missing")

        data_base = base_offset + data_offset + 12  # After DATA header + base_offset field

        # Parse all tracks.
        sequence_size = max(0, data_size - 12)
        data.seek(data_base)
        sequence_bytes = data.read(sequence_size)
        payload_size = len(sequence_bytes.rstrip(b"\x00"))
        command_stream = self._parse_command_stream(
            data, data_base, payload_size,
        )
        tracks = self._build_tracks(labels, command_stream, payload_size)

        # Capture raw data
        data.seek(base_offset)
        raw_data = data.read(file_size)

        return BrseqData(
            version=version,
            labels=labels,
            tracks=tracks,
            command_stream=command_stream,
            raw_data=raw_data,
            data_base_offset=data_base,
        )

    def _read_label_block(self, data: BinaryIO) -> list[Label]:
        block_start = data.tell()

        header = data.read(8)
        if header[:4] != b"LABL":
            return []

        n_labels = struct.unpack(">I", data.read(4))[0]

        # Read label offset table
        label_offsets = struct.unpack(f">{n_labels}I", data.read(4 * n_labels))

        labels = []
        for off in label_offsets:
            data.seek(block_start + 8 + off)  # 8 = block header size
            data_off, name_len = struct.unpack(">II", data.read(8))
            name = data.read(name_len).rstrip(b"\x00").decode("ascii")
            labels.append(Label(name=name, offset=data_off))

        # LABL order is observable and Nintendo's compiler does not
        # necessarily store labels in data-offset order.  Preserve it.
        return labels

    def _parse_command_stream(
            self,
            data: BinaryIO,
            data_base: int,
            payload_size: int,
    ) -> list[Command]:
        commands: list[Command] = []
        data.seek(data_base)
        while data.tell() - data_base < payload_size:
            start = data.tell()
            offset = start - data_base
            cmd = self._decode_command(data)
            end = data.tell()
            if end - data_base > payload_size:
                raise NW4RInvalidFileError(
                    f"BRSEQ command at 0x{offset:X} extends past DATA payload"
                )
            cmd.offset = offset
            data.seek(start)
            cmd.raw_bytes = data.read(end - start)
            commands.append(cmd)
        return commands

    def _build_tracks(
            self,
            labels: list[Label],
            commands: list[Command],
            payload_size: int,
    ) -> dict[str | int, Track]:
        """Build entry-point views without duplicating physical commands."""
        if not commands:
            return {}

        command_offsets = {cmd.offset for cmd in commands}
        boundaries: set[int] = {commands[0].offset}
        boundaries.update(
            label.offset for label in labels if label.offset in command_offsets
        )

        for index, cmd in enumerate(commands):
            mml = cmd.get_mml()
            if mml in (MML.CALL, MML.JUMP) and cmd.args:
                if cmd.args[0] in command_offsets:
                    boundaries.add(cmd.args[0])
            elif mml == MML.OPEN_TRACK and len(cmd.args) >= 2:
                if cmd.args[1] in command_offsets:
                    boundaries.add(cmd.args[1])

            # Nintendo packs otherwise-unreferenced sequence entries back to
            # back.  Start a new anonymous view after a true terminator so
            # every byte remains represented even when only BRSAR INFO points
            # at that entry offset.
            unconditional_end = (
                mml in (MML.FIN, MML.RET)
                or (mml == MML.JUMP and not cmd.has_if)
            ) and not cmd.has_if
            if unconditional_end and index + 1 < len(commands):
                boundaries.add(commands[index + 1].offset)

        ordered_boundaries = sorted(boundaries)
        offset_to_index = {cmd.offset: i for i, cmd in enumerate(commands)}
        labels_by_offset: dict[int, list[Label]] = {}
        for label in labels:
            labels_by_offset.setdefault(label.offset, []).append(label)

        tracks: dict[str | int, Track] = {}
        for boundary_index, start in enumerate(ordered_boundaries):
            first = offset_to_index.get(start)
            if first is None:
                continue
            end = (
                ordered_boundaries[boundary_index + 1]
                if boundary_index + 1 < len(ordered_boundaries)
                else payload_size
            )
            segment = [cmd for cmd in commands[first:] if cmd.offset < end]
            calls: list[int] = []
            jumps: list[int] = []
            opens: list[tuple[int, int]] = []
            for cmd in segment:
                mml = cmd.get_mml()
                if mml == MML.CALL and cmd.args:
                    calls.append(cmd.args[0])
                elif mml == MML.JUMP and cmd.args:
                    jumps.append(cmd.args[0])
                elif mml == MML.OPEN_TRACK and len(cmd.args) >= 2:
                    opens.append((cmd.args[0], cmd.args[1]))

            aliases = labels_by_offset.get(start, [])
            primary_label = aliases[0] if aliases else None
            primary = Track(
                label=primary_label,
                commands=segment,
                start_offset=start,
                end_offset=end,
                calls=calls,
                jumps=jumps,
                opens=opens,
            )
            if aliases:
                tracks[aliases[0].name] = primary
                for alias in aliases[1:]:
                    tracks[alias.name] = Track(
                        label=alias,
                        commands=segment,
                        start_offset=start,
                        end_offset=end,
                        calls=calls,
                        jumps=jumps,
                        opens=opens,
                    )
            else:
                tracks[start] = primary

        return tracks

    def _parse_tracks(
            self,
            data: BinaryIO,
            labels: list[Label],
            data_base: int,
    ) -> dict[str | int, Track]:
        """Parse all tracks from the DATA block."""
        tracks: dict[str | int, Track] = {}
        pending_offsets: dict[int, Track] = {}  # Anonymous tracks to parse
        parsed_offsets: set[int] = set()

        # Create label offset lookup
        label_offsets = {l.offset for l in labels}
        offset_to_label = {l.offset: l for l in labels}

        # Parse named labels first
        for label in labels:
            if label.offset in parsed_offsets:
                # Already parsed (duplicate label)
                existing = tracks.get(label.offset)
                if existing:
                    tracks[label.name] = Track(
                        label=label,
                        commands=existing.commands,
                        start_offset=existing.start_offset,
                        end_offset=existing.end_offset,
                        calls=existing.calls,
                        jumps=existing.jumps,
                        opens=existing.opens,
                    )
                continue

            data.seek(data_base + label.offset)
            track = self._parse_track(data, label, data_base, label_offsets)
            tracks[label.name] = track
            parsed_offsets.add(label.offset)

            # Collect anonymous references
            for off in track.calls + track.jumps:
                if off not in label_offsets and off not in parsed_offsets:
                    pending_offsets[off] = track
            for _, off in track.opens:
                if off not in label_offsets and off not in parsed_offsets:
                    pending_offsets[off] = track

        # Parse anonymous tracks
        while pending_offsets:
            off, caller = pending_offsets.popitem()

            if off in parsed_offsets:
                continue

            # Skip if offset is inside an already-parsed track
            skip = False
            for t in tracks.values():
                if t.start_offset < off < t.end_offset:
                    skip = True
                    break
            if skip:
                continue

            data.seek(data_base + off)
            track = self._parse_track(data, None, data_base, label_offsets)
            track.start_offset = off
            tracks[off] = track
            parsed_offsets.add(off)

            # Collect more anonymous references
            for ref_off in track.calls + track.jumps:
                if ref_off not in label_offsets and ref_off not in parsed_offsets:
                    pending_offsets[ref_off] = track
            for _, ref_off in track.opens:
                if ref_off not in label_offsets and ref_off not in parsed_offsets:
                    pending_offsets[ref_off] = track

        return tracks

    def _parse_track(
            self,
            data: BinaryIO,
            label: Label | None,
            data_base: int,
            label_offsets: set[int],
    ) -> Track:
        start_pos = data.tell()
        start_offset = start_pos - data_base

        commands = []
        calls = []
        jumps = []
        opens = []

        while True:
            cmd_offset = data.tell() - data_base
            cmd = self._decode_command(data)
            cmd.offset = cmd_offset
            commands.append(cmd)

            # Track references
            mml = cmd.get_mml()
            if mml == MML.CALL and cmd.args:
                calls.append(cmd.args[0])
            elif mml == MML.JUMP and cmd.args:
                jumps.append(cmd.args[0])
            elif mml == MML.OPEN_TRACK and len(cmd.args) >= 2:
                opens.append((cmd.args[0], cmd.args[1]))

            # Check termination
            if mml in (MML.FIN, MML.RET) or (mml == MML.JUMP and not cmd.has_if):
                break

            # Check if next position is a named label
            next_offset = data.tell() - data_base
            if next_offset in label_offsets:
                break

        end_offset = data.tell() - data_base

        return Track(
            label=label,
            commands=commands,
            start_offset=start_offset,
            end_offset=end_offset,
            calls=calls,
            jumps=jumps,
            opens=opens,
        )

    def _decode_command(self, data: BinaryIO) -> Command:
        prefixes = []
        time_prefix: Prefix | None = None

        # Read and process prefixes
        b = self._read_u8(data)

        # IF prefix
        if b == MML.IF:
            prefixes.append(Prefix(type=MML.IF))
            b = self._read_u8(data)

        # Time prefix
        if b == MML.TIME:
            time_prefix = Prefix(type=MML.TIME)
            prefixes.append(time_prefix)
            b = self._read_u8(data)
        elif b == MML.TIME_RANDOM:
            time_prefix = Prefix(type=MML.TIME_RANDOM)
            prefixes.append(time_prefix)
            b = self._read_u8(data)
        elif b == MML.TIME_VARIABLE:
            time_prefix = Prefix(type=MML.TIME_VARIABLE)
            prefixes.append(time_prefix)
            b = self._read_u8(data)

        # Random/Variable prefix
        use_prefix_arg = False
        if b == MML.RANDOM:
            prefixes.append(Prefix(type=MML.RANDOM))
            use_prefix_arg = True
            b = self._read_u8(data)
        elif b == MML.VARIABLE:
            prefixes.append(Prefix(type=MML.VARIABLE))
            use_prefix_arg = True
            b = self._read_u8(data)

        # Now b is the actual opcode
        opcode = b
        args = []

        # Handle notes (0x00-0x7F)
        if is_note(opcode):
            velocity = self._read_u8(data)
            if use_prefix_arg:
                # Prefix provides the value
                if prefixes and prefixes[-1].type == MML.RANDOM:
                    prefixes[-1].args = [self._read_s16(data), self._read_s16(data)]
                elif prefixes and prefixes[-1].type == MML.VARIABLE:
                    prefixes[-1].args = [self._read_u8(data)]
                args = [velocity]
            else:
                length = self._read_var_len(data)
                args = [velocity, length]
            return Command(opcode=opcode, args=args, prefixes=prefixes)

        # Handle extended commands
        if opcode == MML.EX_COMMAND:
            ex_opcode = self._read_u8(data)
            args = self._read_extended_args(data, ex_opcode, use_prefix_arg, prefixes)
            cmd = Command(
                opcode=ex_opcode,
                args=args,
                prefixes=prefixes,
                _is_extended=True,
            )
            return cmd

        # Handle regular MML commands
        args = self._read_mml_args(data, opcode, use_prefix_arg, prefixes)
        if time_prefix is not None:
            try:
                mml = MML(opcode)
            except ValueError:
                mml = None
            spec = MML_ARG_SPEC.get(mml, []) if mml is not None else []
            if len(spec) == 1 and spec[0] in {ArgType.U8, ArgType.S8}:
                if time_prefix.type == MML.TIME:
                    time_prefix.args = [self._read_s16(data)]
                elif time_prefix.type == MML.TIME_RANDOM:
                    time_prefix.args = [self._read_s16(data), self._read_s16(data)]
                elif time_prefix.type == MML.TIME_VARIABLE:
                    time_prefix.args = [self._read_u8(data)]
        return Command(opcode=opcode, args=args, prefixes=prefixes)

    def _read_mml_args(
            self,
            data: BinaryIO,
            opcode: int,
            use_prefix_arg: bool,
            prefixes: list[Prefix],
    ) -> list[int]:
        args = []

        try:
            mml = MML(opcode)
        except ValueError:
            # NW4R chooses the argument representation from the opcode's high
            # nibble even for commands it does not act on.
            high = opcode & 0xF0
            if high in (0xB0, 0xC0, 0xD0):
                if use_prefix_arg:
                    self._read_value_prefix_arg(data, prefixes)
                    return []
                return [self._read_u8(data)]
            if high == 0xE0:
                if use_prefix_arg:
                    self._read_value_prefix_arg(data, prefixes)
                    return []
                return [self._read_s16(data)]
            return []

        spec = MML_ARG_SPEC.get(mml, [])

        # Special handling for commands with prefix modifiers
        if use_prefix_arg and spec:
            self._read_value_prefix_arg(data, prefixes)
            return args

        for arg_type in spec:
            if arg_type == ArgType.U8:
                args.append(self._read_u8(data))
            elif arg_type == ArgType.S8:
                args.append(self._read_s8(data))
            elif arg_type == ArgType.U16:
                args.append(self._read_u16(data))
            elif arg_type == ArgType.S16:
                args.append(self._read_s16(data))
            elif arg_type == ArgType.U24:
                args.append(self._read_u24(data))
            elif arg_type == ArgType.VAR_LEN:
                args.append(self._read_var_len(data))

        return args

    def _read_extended_args(
            self,
            data: BinaryIO,
            opcode: int,
            use_prefix_arg: bool,
            prefixes: list[Prefix],
    ) -> list[int]:
        args = []

        try:
            mml = MMLEX(opcode)
        except ValueError:
            high = opcode & 0xF0
            if high == 0xE0:
                if use_prefix_arg:
                    self._read_value_prefix_arg(data, prefixes)
                    return []
                return [self._read_u16(data)]
            if high in (0x80, 0x90):
                first = self._read_u8(data)
                if use_prefix_arg:
                    self._read_value_prefix_arg(data, prefixes)
                    return [first]
                return [first, self._read_s16(data)]
            return []

        spec = MMLEX_ARG_SPEC.get(mml, [ArgType.U8, ArgType.S16])

        # Extended commands can also have prefix modifiers
        if use_prefix_arg:
            # First arg is variable index
            args.append(self._read_u8(data))
            if prefixes and prefixes[-1].type == MML.RANDOM:
                prefixes[-1].args = [self._read_s16(data), self._read_s16(data)]
            elif prefixes and prefixes[-1].type == MML.VARIABLE:
                prefixes[-1].args = [self._read_u8(data)]
            return args

        for arg_type in spec:
            if arg_type == ArgType.U8:
                args.append(self._read_u8(data))
            elif arg_type == ArgType.S16:
                args.append(self._read_s16(data))
            elif arg_type == ArgType.U16:
                args.append(self._read_u16(data))

        return args

    def _read_value_prefix_arg(
            self,
            data: BinaryIO,
            prefixes: list[Prefix],
    ) -> None:
        for prefix in prefixes:
            if prefix.type == MML.RANDOM:
                prefix.args = [self._read_s16(data), self._read_s16(data)]
                return
            if prefix.type == MML.VARIABLE:
                prefix.args = [self._read_u8(data)]
                return

    # Reading helpers
    def _read_u8(self, data: BinaryIO) -> int:
        return struct.unpack(">B", data.read(1))[0]

    def _read_s8(self, data: BinaryIO) -> int:
        return struct.unpack(">b", data.read(1))[0]

    def _read_u16(self, data: BinaryIO) -> int:
        return struct.unpack(">H", data.read(2))[0]

    def _read_s16(self, data: BinaryIO) -> int:
        return struct.unpack(">h", data.read(2))[0]

    def _read_u24(self, data: BinaryIO) -> int:
        b = data.read(3)
        return (b[0] << 16) | (b[1] << 8) | b[2]

    def _read_var_len(self, data: BinaryIO) -> int:
        result = 0
        while True:
            b = self._read_u8(data)
            result = (result << 7) | (b & 0x7F)
            if not (b & 0x80):
                break
        return result
