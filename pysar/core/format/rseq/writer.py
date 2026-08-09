import io
import struct
from typing import BinaryIO

from pysar.core.base import WriterBase
from pysar.core.format.rseq.mml import (
    MML, MMLEX, ArgType, MML_ARG_SPEC, MMLEX_ARG_SPEC, is_note,
)
from pysar.core.model.brseq import BrseqData, Label, Command
from pysar.core.types import ByteOrder


def _round_up(x: int, alignment: int) -> int:
    return (x + alignment - 1) & ~(alignment - 1)


class BrseqWriter(WriterBase):
    """
    Writer for BRSEQ (Binary Revolution Sequence) files.
    """

    VERSION = 0x0100
    HEADER_SIZE = 0x20

    def write(self, model: BrseqData, output: BinaryIO) -> None:
        """Write a BRSEQ file to a binary stream."""

        # Build DATA block
        data_buffer = io.BytesIO()
        label_to_offset = self._write_data_block(data_buffer, model)
        data_bytes = bytearray(data_buffer.getvalue())
        data_size = _round_up(len(data_bytes), 0x20)
        data_bytes.extend(b"\x00" * (data_size - len(data_bytes)))
        struct.pack_into(">I", data_bytes, 4, data_size)
        data_bytes = bytes(data_bytes)

        # Build LABL block
        label_buffer = io.BytesIO()
        self._write_label_block(label_buffer, model.labels, label_to_offset)
        label_bytes = bytearray(label_buffer.getvalue())
        label_size = _round_up(len(label_bytes), 0x20) if label_bytes else 0
        if label_bytes:
            label_bytes.extend(b"\x00" * (label_size - len(label_bytes)))
            struct.pack_into(">I", label_bytes, 4, label_size)
            label_bytes = bytes(label_bytes)
        else:
            label_bytes = b""

        # Calculate offsets
        data_offset = self.HEADER_SIZE
        label_offset = data_offset + data_size if label_bytes else 0
        file_size = data_offset + data_size + label_size

        # Write file header
        output.write(struct.pack(
            ">4sHHIHH IIII",
            b"RSEQ",
            ByteOrder.BIG_ENDIAN,
            model.version or self.VERSION,
            file_size,
            self.HEADER_SIZE,
            2 if label_bytes else 1,  # Number of sections
            data_offset,
            data_size,
            label_offset,
            label_size,
        ))

        # Write blocks
        output.write(data_bytes)
        if label_bytes:
            output.write(label_bytes)

    def _write_data_block(
            self,
            buffer: io.BytesIO,
            model: BrseqData,
    ) -> dict[str, int]:
        # DATA block header
        buffer.write(b"DATA")
        size_pos = buffer.tell()
        buffer.write(struct.pack(">I", 0))  # Size placeholder
        buffer.write(struct.pack(">I", 12))  # Sequence data starts at +0x0c

        commands = self._ordered_commands(model)

        # Text-created BRSEQ uses temporary offsets while parsing. Build real
        # emitted offsets first, then remap all labels and control targets.
        offset_remap: dict[int, int] = {}
        emitted_by_identity: dict[int, int] = {}
        current_offset = 0
        for cmd in commands:
            # Several programmatically-created commands legitimately begin
            # with the default offset 0.  The first occurrence represents
            # label offset 0; do not accidentally remap it to the last one.
            offset_remap.setdefault(int(cmd.offset), current_offset)
            emitted_by_identity[id(cmd)] = current_offset
            current_offset += self._command_size(cmd)

        label_to_offset: dict[str, int] = {}
        # In newly-built/MIDI models, track start offsets and command offsets
        # may all still be zero.  A track label belongs at that track's first
        # emitted command, independent of those placeholders.  This also
        # keeps direct Track.commands edits synchronized with command_stream.
        for key, track in model.tracks.items():
            if not track.commands:
                continue
            emitted = emitted_by_identity.get(id(track.commands[0]))
            if emitted is None:
                continue
            if isinstance(key, str):
                label_to_offset[key] = emitted
            if track.label is not None:
                label_to_offset[track.label.name] = emitted

        for label in model.labels:
            label_to_offset.setdefault(
                label.name,
                offset_remap.get(int(label.offset), int(label.offset)),
            )

        for cmd in commands:
            self._write_command(buffer, cmd, offset_remap)

        # Update size
        end_pos = buffer.tell()
        buffer.seek(size_pos)
        buffer.write(struct.pack(">I", end_pos))
        buffer.seek(end_pos)

        return label_to_offset

    @staticmethod
    def _ordered_commands(model: BrseqData) -> list[Command]:
        if model.command_stream:
            stream_ids = {id(command) for command in model.command_stream}
            track_ids = {
                id(command)
                for track in model.tracks.values()
                for command in track.commands
            }
            if stream_ids == track_ids:
                return list(model.command_stream)

        # Backwards compatibility for programmatically-created models.  A
        # command may occur in several label views, so de-duplicate by object
        # identity first and then retain physical offset order.
        commands: list[Command] = []
        seen_ids: set[int] = set()
        ordered_tracks = sorted(
            enumerate(model.tracks.values()),
            key=lambda item: (item[1].start_offset, item[0]),
        )
        for _, track in ordered_tracks:
            for cmd in track.commands:
                identity = id(cmd)
                if identity in seen_ids:
                    continue
                seen_ids.add(identity)
                commands.append(cmd)
        return commands

    def _write_label_block(
            self,
            buffer: io.BytesIO,
            labels: list[Label],
            label_to_offset: dict[str, int],
    ) -> None:
        """Write the LABL block."""
        if not labels:
            return

        # Block header
        buffer.write(b"LABL")
        size_pos = buffer.tell()
        buffer.write(struct.pack(">I", 0))  # Size placeholder

        # Number of labels
        buffer.write(struct.pack(">I", len(labels)))

        # Reserve space for offset table
        offset_table_pos = buffer.tell()
        for _ in labels:
            buffer.write(struct.pack(">I", 0))

        # Write label entries
        label_entry_offsets = []
        for label in labels:
            entry_offset = buffer.tell() - offset_table_pos + 4  # Relative to after count
            label_entry_offsets.append(entry_offset)

            # Get the actual offset from our mapping
            data_offset = label_to_offset.get(label.name, label.offset)

            encoded_name = label.name.encode("ascii")
            name_len = len(encoded_name)
            name_bytes = encoded_name + b"\x00"

            buffer.write(struct.pack(">II", data_offset, name_len))
            buffer.write(name_bytes)

            # Align to 4 bytes
            padding = (4 - (buffer.tell() % 4)) % 4
            buffer.write(b"\x00" * padding)

        # Go back and write offset table
        end_pos = buffer.tell()
        buffer.seek(offset_table_pos)
        for off in label_entry_offsets:
            buffer.write(struct.pack(">I", off))

        # Update size
        buffer.seek(size_pos)
        buffer.write(struct.pack(">I", end_pos))
        buffer.seek(end_pos)

    def _command_size(self, cmd: Command) -> int:
        temp = io.BytesIO()
        self._write_command(temp, cmd, {})
        return temp.tell()

    def _write_command(self, buffer: io.BytesIO, cmd: Command, offset_remap: dict[int, int] | None = None) -> None:
        """Write a single command."""
        offset_remap = offset_remap or {}

        # Write prefixes
        for prefix in cmd.prefixes:
            if prefix.type == MML.IF:
                buffer.write(struct.pack(">B", MML.IF))

        for prefix in cmd.prefixes:
            if prefix.type == MML.TIME:
                buffer.write(struct.pack(">B", MML.TIME))
            elif prefix.type == MML.TIME_RANDOM:
                buffer.write(struct.pack(">B", MML.TIME_RANDOM))
            elif prefix.type == MML.TIME_VARIABLE:
                buffer.write(struct.pack(">B", MML.TIME_VARIABLE))

        for prefix in cmd.prefixes:
            if prefix.type == MML.RANDOM:
                buffer.write(struct.pack(">B", MML.RANDOM))
            elif prefix.type == MML.VARIABLE:
                buffer.write(struct.pack(">B", MML.VARIABLE))

        # Write opcode
        if is_note(cmd.opcode):
            buffer.write(struct.pack(">B", cmd.opcode))
            # Velocity
            buffer.write(struct.pack(">B", cmd.args[0] if cmd.args else 100))
            # Length (if not using prefix)
            if not cmd.has_random and not cmd.has_variable:
                if len(cmd.args) > 1:
                    self._write_var_len(buffer, cmd.args[1])
                else:
                    self._write_var_len(buffer, 48)
            else:
                # Write prefix args
                for prefix in cmd.prefixes:
                    if prefix.type == MML.RANDOM:
                        buffer.write(struct.pack(">hh", prefix.args[0], prefix.args[1]))
                    elif prefix.type == MML.VARIABLE:
                        buffer.write(struct.pack(">B", prefix.args[0]))
            return

        # Treat this as an extended command only when the reader tagged it.
        # MML and MMLEX share opcodes 0x80-0x8A (WAIT/SETVAR, PRG/ADDVAR,
        # OPEN_TRACK/ORVAR, JUMP/XORVAR, CALL/NOTVAR).  Without the flag
        # we must try MML first so newly-created Commands are not
        # misinterpreted as extended commands.
        if getattr(cmd, '_is_extended', False):
            try:
                mmlex = MMLEX(cmd.opcode)
                buffer.write(struct.pack(">BB", MML.EX_COMMAND, cmd.opcode))
                self._write_extended_args(buffer, cmd)
                return
            except ValueError:
                if cmd.raw_bytes:
                    buffer.write(cmd.raw_bytes)
                else:
                    buffer.write(struct.pack(">BB", MML.EX_COMMAND, cmd.opcode))
                return

        # Regular MML command
        try:
            mml = MML(cmd.opcode)
            buffer.write(struct.pack(">B", cmd.opcode))
            self._write_mml_args(buffer, cmd, offset_remap)
            return
        except ValueError:
            pass

        # Unflagged MMLEX (opcodes that don't overlap with MML)
        try:
            mmlex = MMLEX(cmd.opcode)
            buffer.write(struct.pack(">BB", MML.EX_COMMAND, cmd.opcode))
            self._write_extended_args(buffer, cmd)
            return
        except ValueError:
            pass

        # Unknown opcode.  Its high nibble controls its raw argument width in
        # NW4R, but preserving the reader-captured bytes is safer for future
        # extensions and still permits lossless text/binary pass-through.
        if cmd.raw_bytes:
            buffer.write(cmd.raw_bytes)
        else:
            buffer.write(struct.pack(">B", cmd.opcode))

    def _write_mml_args(self, buffer: io.BytesIO, cmd: Command, offset_remap: dict[int, int]) -> None:
        """Write arguments for a regular MML command."""
        try:
            mml = MML(cmd.opcode)
        except ValueError:
            # Unknown opcode
            if cmd.args:
                buffer.write(struct.pack(">B", cmd.args[0]))
            return

        spec = MML_ARG_SPEC.get(mml, [])

        # Handle prefix modifier args
        if cmd.has_random or cmd.has_variable:
            for prefix in cmd.prefixes:
                if prefix.type == MML.RANDOM:
                    buffer.write(struct.pack(">hh", prefix.args[0], prefix.args[1]))
                elif prefix.type == MML.VARIABLE:
                    buffer.write(struct.pack(">B", prefix.args[0]))
            if len(spec) == 1 and spec[0] in {ArgType.U8, ArgType.S8}:
                for prefix in cmd.prefixes:
                    if prefix.type == MML.TIME and prefix.args:
                        buffer.write(struct.pack(">h", prefix.args[0]))
                    elif prefix.type == MML.TIME_RANDOM and len(prefix.args) >= 2:
                        buffer.write(struct.pack(">hh", prefix.args[0], prefix.args[1]))
                    elif prefix.type == MML.TIME_VARIABLE and prefix.args:
                        buffer.write(struct.pack(">B", prefix.args[0]))
            return

        for i, arg_type in enumerate(spec):
            if i >= len(cmd.args):
                break
            val = cmd.args[i]
            if mml in (MML.JUMP, MML.CALL) and i == 0:
                val = offset_remap.get(int(val), int(val))
            elif mml == MML.OPEN_TRACK and i == 1:
                val = offset_remap.get(int(val), int(val))

            if arg_type == ArgType.U8:
                buffer.write(struct.pack(">B", val & 0xFF))
            elif arg_type == ArgType.S8:
                buffer.write(struct.pack(">b", val))
            elif arg_type == ArgType.U16:
                buffer.write(struct.pack(">H", val))
            elif arg_type == ArgType.S16:
                buffer.write(struct.pack(">h", val))
            elif arg_type == ArgType.U24:
                buffer.write(bytes([(val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF]))
            elif arg_type == ArgType.VAR_LEN:
                self._write_var_len(buffer, val)

        if len(spec) == 1 and spec[0] in {ArgType.U8, ArgType.S8}:
            for prefix in cmd.prefixes:
                if prefix.type == MML.TIME and prefix.args:
                    buffer.write(struct.pack(">h", prefix.args[0]))
                elif prefix.type == MML.TIME_RANDOM and len(prefix.args) >= 2:
                    buffer.write(struct.pack(">hh", prefix.args[0], prefix.args[1]))
                elif prefix.type == MML.TIME_VARIABLE and prefix.args:
                    buffer.write(struct.pack(">B", prefix.args[0]))

    def _write_extended_args(self, buffer: io.BytesIO, cmd: Command) -> None:
        """Write arguments for an extended command."""
        try:
            mmlex = MMLEX(cmd.opcode)
        except ValueError:
            if cmd.args:
                buffer.write(struct.pack(">B", cmd.args[0]))
            return

        spec = MMLEX_ARG_SPEC.get(mmlex, [ArgType.U8, ArgType.S16])

        if cmd.has_random or cmd.has_variable:
            if cmd.args:
                buffer.write(struct.pack(">B", cmd.args[0] & 0xFF))
            for prefix in cmd.prefixes:
                if prefix.type == MML.RANDOM:
                    buffer.write(struct.pack(">hh", prefix.args[0], prefix.args[1]))
                elif prefix.type == MML.VARIABLE:
                    buffer.write(struct.pack(">B", prefix.args[0]))
            return

        for i, arg_type in enumerate(spec):
            if i >= len(cmd.args):
                break
            val = cmd.args[i]

            if arg_type == ArgType.U8:
                buffer.write(struct.pack(">B", val & 0xFF))
            elif arg_type == ArgType.S16:
                buffer.write(struct.pack(">h", val))
            elif arg_type == ArgType.U16:
                buffer.write(struct.pack(">H", val))

    def _write_var_len(self, buffer: io.BytesIO, value: int) -> None:
        """Write a variable-length quantity."""
        if value < 0:
            value = 0

        bytes_list = []
        bytes_list.append(value & 0x7F)
        value >>= 7

        while value > 0:
            bytes_list.append((value & 0x7F) | 0x80)
            value >>= 7

        bytes_list.reverse()
        buffer.write(bytes(bytes_list))
