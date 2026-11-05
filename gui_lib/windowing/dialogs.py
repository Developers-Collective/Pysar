from dataclasses import dataclass


@dataclass(frozen=True)
class FileType:
    name: str
    extensions: tuple[str, ...] = ()


ALL_FILES = FileType("All files")


@dataclass(frozen=True)
class MessageBox:
    message: str
    detail: str = ""
    buttons: tuple[str, ...] = ("OK",)
    default: int = 0
    cancel: int | None = None
    warning: bool = False
    destructive: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.buttons) <= 3:
            raise ValueError("A message box has one to three buttons.")
        if not 0 <= self.default < len(self.buttons):
            raise ValueError(f"There's no button {self.default} to be the default.")
        if self.cancel is not None and not 0 <= self.cancel < len(self.buttons):
            raise ValueError(f"There's no button {self.cancel} to cancel with.")
        if self.destructive is not None and not 0 <= self.destructive < len(self.buttons):
            raise ValueError(f"There's no button {self.destructive} to be the destructive one.")

    @property
    def dismissed(self) -> int:
        return self.default if self.cancel is None else self.cancel

    def order(self, *, default_first: bool) -> list[int]:
        ends = list(dict.fromkeys(index for index in (self.default, self.cancel) if index is not None))
        others = [index for index in range(len(self.buttons)) if index not in ends]
        return [*ends, *others] if default_first else [*others, *reversed(ends)]
