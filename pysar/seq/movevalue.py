class MoveValue:
    def __init__(self, value: int = 0):
        self._origin = value
        self._target = value
        self._frames = 0
        self._counter = 0

    def reset(self, value: int) -> None:
        self._origin = value
        self._target = value
        self._frames = 0
        self._counter = 0

    def set_target(self, value: int, frames: int) -> None:
        self._origin = self.value
        self._target = value
        self._frames = max(0, int(frames))
        self._counter = 0

    def tick(self) -> bool:
        if self._counter < self._frames:
            self._counter += 1
            return True
        return False

    @property
    def value(self) -> int:
        if self._frames == 0 or self._counter >= self._frames:
            return self._target
        return int(self._origin + (self._target - self._origin) * self._counter / self._frames)

    @property
    def target(self) -> int:
        return self._target
