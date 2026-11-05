import ctypes
import hashlib
import logging
import os
import platform
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

RELAUNCHED = "GUI_LIB_RELAUNCHED"
NO_RELAUNCH = "GUI_LIB_NO_RELAUNCH"

# Bump when the bundle's recipe changes, so cached ones are made again
RECIPE = 2

FAT_MAGIC = 0xCAFEBABE
FAT_HEADER = struct.Struct(">II")      # magic, number of architectures (big endian)
FAT_ARCH = struct.Struct(">5I")        # cputype, cpusubtype, offset of the image, size, align
MH_MAGIC_64 = 0xFEEDFACF
MACH_HEADER = struct.Struct("<8I")     # magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved
LOAD_COMMAND = struct.Struct("<II")    # cmd, cmdsize: how every load command starts
VERSION = struct.Struct("<I")

# The load commands that hold the SDK version, and where in them it is
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
SDK_OFFSETS = {
    LC_BUILD_VERSION: 16,       # cmd, cmdsize, platform, minos, sdk, ntools
    LC_VERSION_MIN_MACOSX: 12,  # cmd, cmdsize, version, sdk (older executables, like Python's x86_64 image)
}


def relaunch_if_needed(name: str, icon: Path | None = None) -> None:
    if os.environ.get(RELAUNCHED) or os.environ.get(NO_RELAUNCH) or getattr(sys, "frozen", False):
        return

    if sys.gettrace() is not None or "pydevd" in sys.modules:
        logger.info("a debugger is attached, so the window keeps the compatibility look")
        return

    system = _version(platform.mac_ver()[0])
    if system < (26, 0) or _program_sdk() >= system:
        return

    stub = Path(sys.base_prefix) / "Resources" / "Python.app"
    if not (stub / "Contents" / "MacOS" / "Python").is_file():
        logger.info("this Python has no app stub, so the window keeps the compatibility look")
        return

    try:
        bundle = _bundle(stub, name, icon, system)
    except Exception:
        logger.warning("couldn't make the app bundle, so the window keeps the compatibility look", exc_info=True)
        return

    executable = str(bundle / "Contents" / "MacOS" / "Python")
    environment = {
        **os.environ,
        RELAUNCHED: "1",
        "__PYVENV_LAUNCHER__": sys.executable,
    }
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(executable, [executable, *sys.orig_argv[1:]], environment)


def _bundle(stub: Path, name: str, icon: Path | None, system: tuple[int, int]) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-") or "app"
    folder = Path.home() / "Library" / "Caches" / slug / "runtime"
    bundle = folder / f"{name}.app"
    stamp = folder / "recipe.sha256"

    key = _recipe_key(stub, name, icon, system)
    if bundle.is_dir() and stamp.is_file() and stamp.read_text() == key:
        return bundle

    # Made next to the cached bundle and swapped in once it's done, so a failure
    # halfway leaves neither a broken bundle nor its leftovers behind
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=folder) as temporary:
        staging = Path(temporary) / f"{name}.app"
        shutil.copytree(stub, staging, symlinks=True)
        _brand(staging, name, slug, icon)
        _stamp_sdk(staging / "Contents" / "MacOS" / "Python", system)

        # Changing the executable broke Python's signature, and arm64 won't run it unsigned
        subprocess.run(["codesign", "--force", "--sign", "-", str(staging)], check=True, capture_output=True)

        if bundle.exists():
            shutil.rmtree(bundle)
        staging.rename(bundle)

    stamp.write_text(key)
    return bundle


def _recipe_key(stub: Path, name: str, icon: Path | None, system: tuple[int, int]) -> str:
    source = stub / "Contents" / "MacOS" / "Python"
    icon_changed = icon.stat().st_mtime_ns if icon is not None and icon.is_file() else None
    ingredients = (
        RECIPE,
        # A Python update replaces the stub
        str(source), source.stat().st_mtime_ns, source.stat().st_size,
        name,
        # A macOS update brings a newer look, which the stamp has to ask for
        system,
        str(icon), icon_changed,
    )
    return hashlib.sha256(repr(ingredients).encode()).hexdigest()


def _brand(bundle: Path, name: str, slug: str, icon: Path | None) -> None:
    contents = bundle / "Contents"
    with (contents / "Info.plist").open("rb") as file:
        info = plistlib.load(file)

    info["CFBundleName"] = name
    info["CFBundleDisplayName"] = name
    info["CFBundleIdentifier"] = f"dev.gui-lib.{slug.lower()}"
    info.pop("CFBundleDocumentTypes", None)

    # The Dock shows the icon from the start, before pywebview sets it.
    # Without it the app still gets the new look
    if icon is not None and icon.is_file():
        from .app_config import write_icns

        try:
            write_icns(icon, contents / "Resources" / "AppIcon.icns")
            info["CFBundleIconFile"] = "AppIcon"
        except Exception:
            logger.info("couldn't make the bundle's icon", exc_info=True)

    with (contents / "Info.plist").open("wb") as file:
        plistlib.dump(info, file)


def _stamp_sdk(executable: Path, sdk: tuple[int, int]) -> None:
    data = bytearray(executable.read_bytes())
    stamped = 0

    for start in _images(data):
        magic, _, _, _, command_count, _, _, _ = MACH_HEADER.unpack_from(data, start)
        if magic != MH_MAGIC_64:
            raise ValueError(f"{executable} isn't a 64-bit Mach-O executable")

        # Walk the load commands, each of which says how long it is
        offset = start + MACH_HEADER.size
        for _ in range(command_count):
            command, size = LOAD_COMMAND.unpack_from(data, offset)
            if command in SDK_OFFSETS:
                VERSION.pack_into(data, offset + SDK_OFFSETS[command], _packed_version(sdk))
                stamped += 1
            offset += size

    if not stamped:
        raise ValueError(f"{executable} doesn't say which SDK it was built against")
    executable.write_bytes(bytes(data))


def _images(data: bytes) -> list[int]:
    magic, count = FAT_HEADER.unpack_from(data, 0)
    if magic != FAT_MAGIC:
        return [0]

    archs = (FAT_ARCH.unpack_from(data, FAT_HEADER.size + index * FAT_ARCH.size) for index in range(count))
    return [offset for _, _, offset, _, _ in archs]


def _packed_version(version: tuple[int, int]) -> int:
    """A macOS version (major, minor) the way Mach-O stores it (as 0xMMMMmmpp)"""
    major, minor = version
    return major << 16 | minor << 8


def _program_sdk() -> tuple[int, int]:
    try:
        libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        libsystem.dyld_get_program_sdk_version.restype = ctypes.c_uint32
        value = libsystem.dyld_get_program_sdk_version()
    except (OSError, AttributeError):
        return 0, 0
    return value >> 16, value >> 8 & 0xFF


def _version(text: str) -> tuple[int, int]:
    parts = [int(part) for part in text.split(".")[:2] if part.isdigit()]
    return (parts + [0, 0])[0], (parts + [0, 0])[1]
