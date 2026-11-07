import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import AppKit
import objc

logger = logging.getLogger(__name__)

Entered = Callable[[Any, int, list[Path]], None]

_watched: set[Any] = set()
_lock = threading.Lock()


def watch(view_class: Any, entered: Entered) -> None:
    with _lock:
        if view_class in _watched:
            return
        _watched.add(view_class)

    if "draggingEntered_" in vars(view_class):
        logger.warning("%s has a draggingEntered of its own and isn't watched for dragged files",
                       view_class.__name__)
        return

    def dragging_entered(self: Any, info: Any) -> int:
        try:
            entered(self, info.draggingSequenceNumber(), file_paths(info.draggingPasteboard()))
        except Exception:
            logger.exception("couldn't read the dragged files")
        return objc.super(view_class, self).draggingEntered_(info)

    objc.classAddMethods(view_class, [objc.selector(dragging_entered, selector=b"draggingEntered:",
                                                    signature=b"Q@:@")])


def file_paths(pasteboard: Any) -> list[Path]:
    options = {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}
    paths = []
    for url in pasteboard.readObjectsForClasses_options_([AppKit.NSURL], options) or []:
        # Finder drags file reference URLs
        url = url.filePathURL()
        if url is not None and url.path():
            paths.append(Path(url.path()))
    return paths
