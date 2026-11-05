import logging
import struct
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# make the dock Icon adjusted to all other dock icons because it's too big
ICON_CONTENT_SCALE = 0.82
ICNS_IMAGES = ((b"ic07", 128), (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024), (b"ic13", 256), (b"ic14", 512))


def apply_name(name: str) -> None:
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
            info["CFBundleDisplayName"] = name
    except Exception:
        logger.warning("couldn't set the app's name", exc_info=True)


def dock_icon(icon: Path | None) -> Path | None:
    if icon is None or not icon.is_file():
        return icon

    cached = Path(tempfile.gettempdir()) / f"{icon.stem}-dock-{int(ICON_CONTENT_SCALE * 100)}-v2.png"
    try:
        if cached.exists() and cached.stat().st_mtime_ns >= icon.stat().st_mtime_ns:
            return cached

        image, width, height = _load(icon)
        png = _padded_png(image, width, height)
        if png is not None:
            cached.write_bytes(png)
            return cached
    except Exception:
        logger.debug("couldn't pad the Dock icon", exc_info=True)
    return icon


def write_icns(icon: Path, target: Path) -> None:
    image, _, _ = _load(icon)
    chunks = b""
    for kind, size in ICNS_IMAGES:
        png = _padded_png(image, size, size)
        if png is None:
            raise ValueError(f"couldn't draw {icon} at {size}px")
        chunks += kind + struct.pack(">I", len(png) + 8) + png
    target.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + chunks)


def _load(icon: Path) -> tuple[Any, int, int]:
    from AppKit import NSBitmapImageRep, NSImage

    image = NSImage.alloc().initByReferencingFile_(str(icon))
    source = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation()) if image is not None else None
    if source is None:
        raise ValueError(f"{icon} isn't an image")
    return image, int(source.pixelsWide()), int(source.pixelsHigh())


def _padded_png(image: Any, width: int, height: int) -> bytes | None:
    from AppKit import NSBitmapImageFileTypePNG, NSBitmapImageRep, NSCalibratedRGBColorSpace, NSGraphicsContext

    # Drawn into a bitmap of exactly these pixels. Drawing into an NSImage would follow
    # the screen's scale and make a 1024px logo 2048px on Retina so bigger than an app
    # icon may be
    bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, width, height, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap))
        inner_width, inner_height = width * ICON_CONTENT_SCALE, height * ICON_CONTENT_SCALE
        image.drawInRect_((((width - inner_width) / 2, (height - inner_height) / 2), (inner_width, inner_height)))
    finally:
        NSGraphicsContext.restoreGraphicsState()

    png = bitmap.representationUsingType_properties_(NSBitmapImageFileTypePNG, {})
    return bytes(png) if png is not None else None
