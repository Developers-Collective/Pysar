class NW4RError(Exception):
    """Base exception for all NW4R-related errors."""
    pass


class NW4RInternalError(NW4RError):
    """Indicates an error in the internal API occurred."""
    pass


class NW4RInvalidFileError(NW4RError):
    """Indicates that an invalid or unexpected file was read."""
    pass


class NW4RUnsupportedVersionError(NW4RError):
    """Indicates an unsupported version for a NW4R file."""
    pass


class NW4RByteOrderError(NW4RError):
    """Indicates an invalid byte order for a NW4R file."""
    pass


class BrsarError(NW4RError):
    """BRSAR-specific errors (sound not found, type mismatch, etc.)."""
    pass


class ArchiveDumpCancelled(NW4RError):
    """Raised internally when a cooperative archive dump is aborted."""
    pass
