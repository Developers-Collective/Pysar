from pathlib import Path

from pysar.core.exceptions import BrsarError
from pysar.core.format.rstm import Brstm
from pysar.core.model.brsar import SeqSoundInfo, SoundType, WaveSoundInfo
from pysar.seq.types import PlaybackContext


def make_playback_context(archive, name: str) -> PlaybackContext:
    entry = archive.get_sound_entry(name)
    if entry is None:
        raise BrsarError(f'Sound "{name}" not found')

    archive_volume = max(0.0, min(1.0, entry.volume / 127.0))

    if entry.sound_type == SoundType.SEQ:
        seq_info: SeqSoundInfo = entry.sound_info
        brseq = archive.get_seq(entry.file_index)
        brbnk = archive.get_bank(seq_info.bank_index)
        brwar = archive.get_bank_war(seq_info.bank_index)
        start_label, start_offset = archive._resolve_seq_start(brseq, name, seq_info.seq_label_offset)
        default_programs = archive._resolve_default_program(name, entry, brseq, start_label, start_offset)
        if isinstance(default_programs, int):
            program_map = {0: default_programs}
        else:
            program_map = default_programs or {}
        return PlaybackContext(
            name=name,
            sound_type=entry.sound_type,
            entry=entry,
            archive_volume=archive_volume,
            brseq=brseq,
            brbnk=brbnk,
            brwar=brwar,
            start_label=start_label,
            start_offset=start_offset,
            default_programs=program_map,
        )

    if entry.sound_type == SoundType.WAVE:
        wave_info: WaveSoundInfo = entry.sound_info
        brwsd = archive.get_wsd(entry.file_index)
        brwar = archive.get_wave_war(entry.file_index)
        return PlaybackContext(
            name=name,
            sound_type=entry.sound_type,
            entry=entry,
            archive_volume=archive_volume,
            brwsd=brwsd,
            brwar=brwar,
            extras={"wave_sound_index": wave_info.wave_index},
        )

    if entry.sound_type == SoundType.STRM:
        raw = archive._resolve_file_raw(entry.file_index)
        brstm = None
        if raw is not None:
            brstm = Brstm.from_bytes(raw)
        else:
            file_entry = archive.data.file_entries[entry.file_index]
            if file_entry.external_file_path:
                path = Path(file_entry.external_file_path)
                if path.exists():
                    brstm = Brstm.open(path)
        if brstm is None:
            raise BrsarError(f'Could not resolve BRSTM for stream sound "{name}"')
        return PlaybackContext(
            name=name,
            sound_type=entry.sound_type,
            entry=entry,
            archive_volume=archive_volume,
            brstm=brstm,
        )

    raise BrsarError(f'Unsupported sound type for "{name}": {entry.sound_type!r}')

