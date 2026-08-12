"""
RSEQ text format (assembly-like representation).

This module handles conversion between binary BRSEQ and human-readable
RSEQ text format.

Example RSEQ text:
    ;; Sound effect sequence
    main:
        alloc_track 0000000000000011
        tempo 120
        open_track 1, track1

    track0:
        prg 0
        cn4 100, 48
        wait 48
        fin

    track1:
        prg 1
        loop_start 4
        gn4 80, 24
        wait 24
        loop_end
        ret
"""
import ast
import operator
import re
from io import BytesIO, StringIO
from typing import TextIO

from pysar.core.model.brseq import BrseqData, Label, Track, Command, Prefix
from pysar.core.format.rseq.mml import (
    MML, MMLEX, ArgType,
    is_note, note_to_name, name_to_note,
    MML_ARG_SPEC, MMLEX_ARG_SPEC, PREFIXABLE_COMMANDS,
)


_COMMAND_ALIASES = {
    "alloctrack": "alloc_track",
    "opentrack": "open_track",
    "notewait": "note_wait",
}

_GLUED_COMMAND_ALIASES = {
    "tieon": "tie_on",
    "tieoff": "tie_off",
    "notewait_on": "note_wait_on",
    "notewait_off": "note_wait_off",
    # Nintendo's source dialect uses porta <note> to select/enable the
    # source key and porta_off/porta_on for the separate PORTA_SW opcode.
    "porta_on": "porta_sw_on",
    "porta_off": "porta_sw_off",
}

_CONSTANTS = {
    **{f"TRACK_{index}": 1 << index for index in range(16)},
    "MOD_TYPE_PITCH": 0,
    "MOD_TYPE_VOLUME": 1,
    "MOD_TYPE_PAN": 2,
    "MUTE_OFF": 0,
    "MUTE_NO_STOP": 1,
    "MUTE_RELEASE": 2,
    "MUTE_STOP": 3,
}

_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Div: operator.floordiv,
    ast.Mod: operator.mod,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_,
    ast.BitXor: operator.xor,
}
_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Invert: operator.invert,
}

_VALUE_PREFIX_TYPES = (MML.RANDOM, MML.VARIABLE)
_TIME_PREFIX_TYPES = (MML.TIME, MML.TIME_RANDOM, MML.TIME_VARIABLE)


def _strip_comment(line: str) -> str:
    """Remove BRSEQ text comments before parsing."""
    cuts = [index for index in (line.find("#"), line.find(";")) if index >= 0]
    if not cuts:
        return line
    return line[:min(cuts)]


def _prefix_suffix_and_args(prefixes: list[Prefix]) -> tuple[str, list[int]]:
    suffix = ""
    for prefix in prefixes:
        if prefix.type == MML.IF:
            suffix += "_if"
        elif prefix.type == MML.RANDOM:
            suffix += "_r"
        elif prefix.type == MML.VARIABLE:
            suffix += "_v"
        elif prefix.type == MML.TIME:
            suffix += "_t"
        elif prefix.type == MML.TIME_RANDOM:
            suffix += "_tr"
        elif prefix.type == MML.TIME_VARIABLE:
            suffix += "_tv"
    # Binary order is primary (RANDOM/VARIABLE) then secondary TIME.  Keep
    # text arguments in that order even though the prefix opcode bytes occur
    # TIME-before-RANDOM.
    args: list[int] = []
    for prefix_type in (*_VALUE_PREFIX_TYPES, *_TIME_PREFIX_TYPES):
        for prefix in prefixes:
            if prefix.type == prefix_type:
                args.extend(prefix.args)
    return suffix, args


def _coerce_legacy_prefix_args(name: str, args: list[int], prefixes: list[Prefix], spec: list[ArgType]) -> tuple[str, list[int]]:
    """Display old malformed text objects as explicit prefix commands."""
    if prefixes or len(args) <= len(spec):
        return name, args
    if len(spec) == 1 and spec[0] == ArgType.VAR_LEN and len(args) == 2:
        return f"{name}_r", args
    if len(spec) == 1 and spec[0] in (ArgType.U8, ArgType.S8):
        if len(args) == 2:
            return f"{name}_t", args
        if len(args) == 3:
            return f"{name}_tr", args
    return name, args


def _split_prefixed_args(args: list[int], prefixes: list[Prefix], base_arg_count: int) -> list[int]:
    command_args = list(args[:base_arg_count])
    rest = list(args[base_arg_count:])
    for prefix_type in (*_VALUE_PREFIX_TYPES, *_TIME_PREFIX_TYPES):
        for prefix in prefixes:
            if prefix.type != prefix_type:
                continue
            if prefix.type in (MML.RANDOM, MML.TIME_RANDOM):
                prefix.args = rest[:2]
                rest = rest[2:]
            elif prefix.type in (MML.VARIABLE, MML.TIME, MML.TIME_VARIABLE):
                prefix.args = rest[:1]
                rest = rest[1:]
    if rest:
        raise ValueError(f"Too many prefix arguments: {rest}")
    return command_args


def _validate_command_arity(
        command_name: str,
        line_no: int,
        args: list[int],
        prefixes: list[Prefix],
        base_arg_count: int,
) -> None:
    expected_prefix_args = {
        MML.RANDOM: 2,
        MML.VARIABLE: 1,
        MML.TIME: 1,
        MML.TIME_RANDOM: 2,
        MML.TIME_VARIABLE: 1,
        MML.IF: 0,
    }
    expected = int(base_arg_count)
    actual = len(args)
    for prefix in prefixes:
        prefix_expected = expected_prefix_args.get(prefix.type, 0)
        expected += prefix_expected
        actual += len(prefix.args)

    if actual == expected and len(args) == base_arg_count and all(
            len(prefix.args) == expected_prefix_args.get(prefix.type, 0)
            for prefix in prefixes
    ):
        return

    noun = "argument" if expected == 1 else "arguments"
    raise ValueError(
        f"Line {line_no}: '{command_name}' expects {expected} {noun}, got {actual}"
    )


def to_text(data: BrseqData, include_offsets: bool = False) -> str:
    """
    Convert BRSEQ data to RSEQ text format.

    Args:
        data: The BRSEQ data to convert
        include_offsets: If True, include offset comments

    Returns:
        RSEQ text string
    """
    output = StringIO()

    if data.command_stream:
        commands = list(data.command_stream)
    else:
        commands = []
        seen_ids: set[int] = set()
        for track in data.tracks.values():
            for cmd in track.commands:
                if id(cmd) not in seen_ids:
                    seen_ids.add(id(cmd))
                    commands.append(cmd)
        commands.sort(key=lambda command: command.offset)

    if not commands:
        return ""

    labels_by_offset: dict[int, list[Label]] = {}
    labl_indices = {id(label): index for index, label in enumerate(data.labels)}
    for label in data.labels:
        labels_by_offset.setdefault(label.offset, []).append(label)

    entry_offsets: set[int] = {commands[0].offset}
    for index, cmd in enumerate(commands):
        mml = cmd.get_mml()
        if mml in (MML.JUMP, MML.CALL) and cmd.args:
            entry_offsets.add(cmd.args[0])
        elif mml == MML.OPEN_TRACK and len(cmd.args) >= 2:
            entry_offsets.add(cmd.args[1])
        if (
                not cmd.has_if
                and mml in (MML.FIN, MML.RET, MML.JUMP)
                and index + 1 < len(commands)
        ):
            entry_offsets.add(commands[index + 1].offset)

    used_names = {label.name for label in data.labels}
    generated_names: dict[int, str] = {}
    for offset in sorted(entry_offsets):
        if offset in labels_by_offset:
            continue
        candidate = f"_entry_{offset:06X}"
        suffix = 2
        while candidate in used_names:
            candidate = f"_entry_{offset:06X}_{suffix}"
            suffix += 1
        generated_names[offset] = candidate
        used_names.add(candidate)

    # Prefer the first public label at an offset; otherwise use the local
    # generated entry label for CALL/JUMP/OPEN_TRACK operands.
    offset_to_label = {
        offset: (
            f"::{labels[0].name}"
            if labels[0].name.startswith("_")
            else labels[0].name
        )
        for offset, labels in labels_by_offset.items()
    }
    offset_to_label.update(generated_names)

    for cmd in commands:
        public_labels = labels_by_offset.get(cmd.offset, [])
        local_name = generated_names.get(cmd.offset)
        if public_labels or local_name:
            if output.tell() > 0:
                output.write("\n")
            for label in public_labels:
                display_name = (
                    f"::{label.name}" if label.name.startswith("_")
                    else label.name
                )
                output.write(
                    f"{display_name}: ;; @labl-index {labl_indices[id(label)]}\n"
                )
            if local_name:
                output.write(f"{local_name}:\n")

        line = _format_command(cmd, offset_to_label)
        if include_offsets:
            output.write(f"    {line:40} ;; @0x{cmd.offset:04X}\n")
        else:
            output.write(f"    {line}\n")

    return output.getvalue()


def _write_track(
        output: TextIO,
        track: Track,
        offset_to_label: dict[int, str],
        include_offsets: bool,
) -> None:
    # Label
    output.write(f"{track.name}:\n")

    for cmd in track.commands:
        line = _format_command(cmd, offset_to_label)

        if include_offsets:
            output.write(f"    {line:40} ;; @0x{cmd.offset:04X}\n")
        else:
            output.write(f"    {line}\n")


def _format_command(cmd: Command, offset_to_label: dict[int, str]) -> str:

    prefix_str, prefix_args = _prefix_suffix_and_args(cmd.prefixes)

    # Get command name
    if is_note(cmd.opcode):
        name = note_to_name(cmd.opcode) + prefix_str
        # Note args: velocity, length
        args = [*cmd.args, *prefix_args]
        if args:
            args_str = ", ".join(map(str, args))
            return f"{name} {args_str}"
        return name

    if cmd.is_extended:
        try:
            mml = MMLEX(cmd.opcode)
            name = mml.name.lower() + prefix_str
            display_name, display_args = _coerce_legacy_prefix_args(name, list(cmd.args), cmd.prefixes, MMLEX_ARG_SPEC.get(mml, []))
            name = display_name
        except ValueError:
            if cmd.raw_bytes:
                return f"raw 0x{cmd.raw_bytes.hex().upper()}"
            name = f"0x{cmd.opcode:02X}" + prefix_str
            args = [*cmd.args, *prefix_args]
            if args:
                return f"{name} {', '.join(map(str, args))}"
            return name
    else:
        try:
            mml = MML(cmd.opcode)
            name = mml.name.lower() + prefix_str
            display_name, display_args = _coerce_legacy_prefix_args(name, list(cmd.args), cmd.prefixes, MML_ARG_SPEC.get(mml, []))
            name = display_name
        except ValueError:
            try:
                mml = MMLEX(cmd.opcode)
                name = mml.name.lower() + prefix_str
                display_name, display_args = _coerce_legacy_prefix_args(name, list(cmd.args), cmd.prefixes, MMLEX_ARG_SPEC.get(mml, []))
                name = display_name
            except ValueError:
                if cmd.raw_bytes:
                    return f"raw 0x{cmd.raw_bytes.hex().upper()}"
                name = f"0x{cmd.opcode:02X}" + prefix_str
                args = [*cmd.args, *prefix_args]
                if args:
                    return f"{name} {', '.join(map(str, args))}"
                return name

    # Special formatting for certain commands
    mml = cmd.get_mml()

    if mml == MML.ALLOC_TRACK and cmd.args:
        # Format as binary
        return f"{name} {cmd.args[0]:016b}"

    if mml in (MML.JUMP, MML.CALL) and cmd.args:
        # Resolve label
        off = cmd.args[0]
        label = offset_to_label.get(off, f"0x{off:X}")
        return f"{name} {label}"

    if mml == MML.OPEN_TRACK and len(cmd.args) >= 2:
        # Track number and label
        track_no = cmd.args[0]
        off = cmd.args[1]
        label = offset_to_label.get(off, f"0x{off:X}")
        return f"{name} {track_no}, {label}"

    if mml == MML.PORTA and cmd.args:
        return f"{name} {note_to_name(cmd.args[0])}"

    if mml in (
            MML.MONOPHONIC, MML.NOTE_WAIT, MML.DAMPER, MML.TIE,
            MML.PORTA_SW,
    ):
        # on/off style
        enabled = bool(cmd.args and cmd.args[0])
        if mml == MML.NOTE_WAIT:
            return f"notewait_{'on' if enabled else 'off'}"
        if mml == MML.TIE:
            return f"tie{'on' if enabled else 'off'}"
        if mml == MML.PORTA_SW:
            return f"porta_{'on' if enabled else 'off'}"
        return f"{name}_{'on' if enabled else 'off'}"

    # Default formatting
    args = [*display_args, *prefix_args]
    if args:
        return f"{name} {', '.join(map(str, args))}"
    return name


# =============================================================================
# RSEQ Text Parser
# =============================================================================

def from_text(text: str) -> BrseqData:
    """
    Parse RSEQ text format to BRSEQ data.

    Args:
        text: RSEQ text string

    Returns:
        BrseqData ready for binary encoding
    """
    lines = text.split("\n")

    # First pass: assign exact byte offsets.  Branch operands are fixed-width,
    # so unresolved forward labels can safely use a zero placeholder here.
    label_offsets: dict[str, int] = {}
    current_offset = 0
    scope = ""
    saw_label = False
    for line_no, source_line in enumerate(lines, 1):
        line = _strip_comment(source_line).strip()
        if not line:
            continue
        declaration = _parse_label_declaration(line)
        if declaration is not None:
            name, is_local = declaration
            if not is_local:
                scope = name
            symbol = _qualify_label(name, is_local, scope)
            if symbol in label_offsets:
                raise ValueError(f"Line {line_no}: Duplicate label '{name}'")
            label_offsets[symbol] = current_offset
            saw_label = True
            continue
        if not saw_label:
            raise ValueError(f"Line {line_no}: Command outside of label")
        cmd = _parse_command(
            line, label_offsets, line_no,
            allow_unresolved=True, scope=scope,
        )
        current_offset += _estimate_command_size(cmd)

    labels: list[Label] = []
    label_order: dict[int, int] = {}
    command_stream: list[Command] = []
    current_offset = 0
    scope = ""
    saw_label = False
    for line_no, source_line in enumerate(lines, 1):
        line = _strip_comment(source_line).strip()
        if not line:
            continue
        declaration = _parse_label_declaration(line)
        if declaration is not None:
            name, is_local = declaration
            if not is_local:
                scope = name
                label = Label(name=name, offset=current_offset)
                labels.append(label)
                order_match = re.search(
                    r"@labl-index\s+(\d+)", source_line,
                    flags=re.IGNORECASE,
                )
                if order_match:
                    label_order[id(label)] = int(order_match.group(1))
            saw_label = True
            continue
        if not saw_label:
            raise ValueError(f"Line {line_no}: Command outside of label")
        cmd = _parse_command(line, label_offsets, line_no, scope=scope)
        cmd.offset = current_offset
        command_stream.append(cmd)
        current_offset += _estimate_command_size(cmd)

    if label_order:
        fallback = max(label_order.values(), default=-1) + 1
        physical_order = {id(label): index for index, label in enumerate(labels)}
        labels.sort(key=lambda label: (
            label_order.get(id(label), fallback + physical_order[id(label)]),
            physical_order[id(label)],
        ))

    # Reuse the binary reader's entry-point view builder so the player sees
    # the same flat stream regardless of whether input was binary or text.
    from pysar.core.format.rseq.reader import BrseqReader
    tracks = BrseqReader()._build_tracks(labels, command_stream, current_offset)

    return BrseqData(
        version=0x0100,
        labels=labels,
        tracks=tracks,
        command_stream=command_stream,
    )


def _parse_label_declaration(line: str) -> tuple[str, bool] | None:
    if not line.endswith(":"):
        return None
    name = line[:-1].strip()
    force_global = name.startswith("::")
    if force_global:
        name = name[2:]
    if not name:
        raise ValueError("Empty label")
    return name, name.startswith("_") and not force_global


def _qualify_label(name: str, is_local: bool, scope: str) -> str:
    if not is_local:
        return name
    return f"{scope}::{name}"


def _parse_command(
        line: str,
        label_offsets: dict[str, int],
        line_no: int,
        allow_unresolved: bool = False,
        scope: str = "",
) -> Command:
    """Parse a single command line."""
    # Split command and arguments
    parts = line.split(None, 1)
    original_cmd = parts[0].lower()
    cmd_str = _GLUED_COMMAND_ALIASES.get(original_cmd, original_cmd)
    args_str = parts[1] if len(parts) > 1 else ""

    if cmd_str in ("raw", ".raw"):
        try:
            compact = re.sub(r"[^0-9a-fA-F]", "", args_str.removeprefix("0x"))
            if not compact or len(compact) % 2:
                raise ValueError("raw command needs an even number of hex digits")
            raw = bytes.fromhex(compact)
            from pysar.core.format.rseq.reader import BrseqReader
            stream = BytesIO(raw)
            command = BrseqReader()._decode_command(stream)
            if stream.tell() != len(raw):
                raise ValueError("raw command contains more than one opcode")
            command.raw_bytes = raw
            return command
        except (IndexError, ValueError) as exc:
            raise ValueError(f"Line {line_no}: Invalid raw command: {exc}") from exc

    # Parse prefixes
    found_prefixes: dict[MML, Prefix] = {}
    suffixes = (
        ("_if", MML.IF),
        ("_tr", MML.TIME_RANDOM),
        ("_tv", MML.TIME_VARIABLE),
        ("_t", MML.TIME),
        ("_r", MML.RANDOM),
        ("_v", MML.VARIABLE),
    )
    while True:
        matched = False
        for suffix, prefix_type in suffixes:
            if cmd_str.endswith(suffix):
                if prefix_type in found_prefixes:
                    raise ValueError(
                        f"Line {line_no}: Duplicate prefix '{suffix[1:]}'"
                    )
                found_prefixes[prefix_type] = Prefix(type=prefix_type)
                cmd_str = cmd_str[:-len(suffix)]
                matched = True
                break
        if not matched:
            break
    prefixes = []
    for prefix_type in (
            MML.IF,
            MML.TIME, MML.TIME_RANDOM, MML.TIME_VARIABLE,
            MML.RANDOM, MML.VARIABLE,
    ):
        if prefix_type in found_prefixes:
            prefixes.append(found_prefixes[prefix_type])

    cmd_str = _COMMAND_ALIASES.get(cmd_str, cmd_str)

    # Handle on/off suffix
    is_on = None
    if cmd_str.endswith("_on"):
        cmd_str = cmd_str[:-3]
        is_on = True
    elif cmd_str.endswith("_off"):
        cmd_str = cmd_str[:-4]
        is_on = False

    # Parse arguments
    try:
        compact_alloc = args_str.strip().replace(" ", "")
        if (
                cmd_str == "alloc_track"
                and compact_alloc
                and all(char in "01" for char in compact_alloc)
        ):
            args = [int(compact_alloc, 2)]
        else:
            args = _parse_args(
                args_str, label_offsets, allow_unresolved, scope=scope,
            )
    except ValueError as exc:
        raise ValueError(f"Line {line_no}: {exc}") from exc

    # Check if it's a note
    try:
        note = name_to_note(cmd_str)
        if prefixes:
            has_value_prefix = any(
                prefix.type in _VALUE_PREFIX_TYPES for prefix in prefixes
            )
            args = _split_prefixed_args(
                args, prefixes,
                min(1 if has_value_prefix else 2, len(args)),
            )
        has_value_prefix = any(
            prefix.type in _VALUE_PREFIX_TYPES for prefix in prefixes
        )
        _validate_command_arity(
            original_cmd, line_no, args, prefixes,
            1 if has_value_prefix else 2,
        )
        return Command(opcode=note, args=args, prefixes=prefixes)
    except ValueError:
        pass

    # Check MML commands
    try:
        mml = MML[cmd_str.upper()]

        # Handle on/off commands
        if is_on is not None:
            args = [1 if is_on else 0]

        # Handle binary alloc_track
        if mml == MML.ALLOC_TRACK and args_str:
            # Parse binary string
            binary_str = args_str.strip().replace(" ", "")
            if all(c in "01" for c in binary_str):
                args = [int(binary_str, 2)]

        spec = MML_ARG_SPEC.get(mml, [])
        if not prefixes and mml in PREFIXABLE_COMMANDS:
            if len(spec) == 1 and spec[0] == ArgType.VAR_LEN and len(args) == 2:
                prefixes.append(Prefix(type=MML.RANDOM, args=list(args)))
                args = []
            elif len(spec) == 1 and spec[0] in (ArgType.U8, ArgType.S8):
                if len(args) == 2:
                    prefixes.append(Prefix(type=MML.TIME, args=[args[1]]))
                    args = args[:1]
                elif len(args) == 3:
                    prefixes.append(Prefix(type=MML.TIME_RANDOM, args=args[1:3]))
                    args = args[:1]

        if prefixes:
            has_value_prefix = any(prefix.type in (MML.RANDOM, MML.VARIABLE) for prefix in prefixes)
            has_time_prefix = any(prefix.type in (MML.TIME, MML.TIME_RANDOM, MML.TIME_VARIABLE) for prefix in prefixes)
            if has_value_prefix and spec:
                base_arg_count = 0
            else:
                base_arg_count = len(spec)
            if has_time_prefix and not has_value_prefix and len(spec) == 1 and spec[0] in (ArgType.U8, ArgType.S8):
                base_arg_count = 1
            args = _split_prefixed_args(args, prefixes, min(base_arg_count, len(args)))

        has_value_prefix = any(
            prefix.type in _VALUE_PREFIX_TYPES for prefix in prefixes
        )
        _validate_command_arity(
            original_cmd, line_no, args, prefixes,
            0 if has_value_prefix else len(spec),
        )

        return Command(opcode=mml.value, args=args, prefixes=prefixes)
    except KeyError:
        pass

    # Check MMLEX commands
    try:
        mmlex = MMLEX[cmd_str.upper()]
        if prefixes:
            has_value_prefix = any(prefix.type in (MML.RANDOM, MML.VARIABLE) for prefix in prefixes)
            base_arg_count = (
                (0 if mmlex == MMLEX.USERPROC else 1)
                if has_value_prefix
                else len(MMLEX_ARG_SPEC.get(mmlex, []))
            )
            args = _split_prefixed_args(args, prefixes, min(base_arg_count, len(args)))
        has_value_prefix = any(
            prefix.type in _VALUE_PREFIX_TYPES for prefix in prefixes
        )
        _validate_command_arity(
            original_cmd, line_no, args, prefixes,
            (0 if mmlex == MMLEX.USERPROC else 1)
            if has_value_prefix else len(MMLEX_ARG_SPEC.get(mmlex, [])),
        )
        command = Command(opcode=mmlex.value, args=args, prefixes=prefixes)
        command._is_extended = True
        return command
    except KeyError:
        pass

    # Unknown command - try hex
    if cmd_str.startswith("0x"):
        opcode = int(cmd_str, 16)
        return Command(opcode=opcode, args=args, prefixes=prefixes)

    raise ValueError(f"Line {line_no}: Unknown command '{original_cmd}'")


def _parse_args(
        args_str: str,
        label_offsets: dict[str, int],
        allow_unresolved: bool = False,
        scope: str = "",
) -> list[int]:
    """Parse comma-separated arguments."""
    if not args_str.strip():
        return []

    args = []
    for part in args_str.split(","):
        part = part.strip()
        if not part:
            continue

        symbol = part
        if symbol.startswith("::"):
            symbol = symbol[2:]
        elif symbol.startswith("_"):
            local_symbol = _qualify_label(symbol, True, scope)
            if local_symbol in label_offsets:
                symbol = local_symbol

        if symbol in label_offsets:
            args.append(label_offsets[symbol])
            continue

        # Nintendo accepts note names as numeric key arguments (notably
        # ``porta cn4``).
        try:
            args.append(name_to_note(part))
            continue
        except (TypeError, ValueError):
            pass

        try:
            args.append(_evaluate_expression(part))
        except (SyntaxError, TypeError, ValueError, ZeroDivisionError):
            if allow_unresolved:
                args.append(0)
            else:
                raise ValueError(f"Unknown value or label '{part}'")

    return args


def _evaluate_expression(source: str) -> int:
    """Evaluate the integer-only expression subset accepted by seqconv."""
    node = ast.parse(source, mode="eval").body

    def evaluate(value: ast.AST) -> int:
        if isinstance(value, ast.Constant) and isinstance(value.value, int):
            return int(value.value)
        if isinstance(value, ast.Name) and value.id in _CONSTANTS:
            return _CONSTANTS[value.id]
        if isinstance(value, ast.BinOp) and type(value.op) in _BINARY_OPERATORS:
            return int(_BINARY_OPERATORS[type(value.op)](
                evaluate(value.left), evaluate(value.right),
            ))
        if isinstance(value, ast.UnaryOp) and type(value.op) in _UNARY_OPERATORS:
            return int(_UNARY_OPERATORS[type(value.op)](evaluate(value.operand)))
        raise ValueError(f"Unsupported expression '{source}'")

    return evaluate(node)


def _estimate_command_size(cmd: Command) -> int:
    """Return the exact encoded size (including multi-byte VLQ values)."""
    from pysar.core.format.rseq.writer import BrseqWriter
    return BrseqWriter()._command_size(cmd)
