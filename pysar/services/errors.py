class ServiceError(Exception):
    """Base error for the application service layer."""


class NoProjectOpenError(ServiceError):
    """Raised when a service needs an open archive session."""


class InvalidSelectionError(ServiceError):
    """Raised when the requested sound, bank, or group does not exist."""


class SaveFailedError(ServiceError):
    """Raised when a project save operation cannot be completed."""


class PlaybackFailedError(ServiceError):
    """Raised when preview playback or rendering fails."""

