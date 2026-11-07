from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
Finish = Callable[[bool], None]


@dataclass(frozen=True)
class DragOut:
    label: str
    folder_dropped: Callable[[Path, Finish], None]
    render: Callable[[Path], Sequence[Path]] | None = None
