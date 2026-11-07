import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import AppKit
import objc

logger = logging.getLogger(__name__)

Entered = Callable[[Any, int, list[Path]], None]
# Whether a drag's source is one of the app's own drags out (drag_out.owns)
Owns = Callable[[Any], bool]

_watched: set[Any] = set()
_lock = threading.Lock()


def watch(view_class: Any, entered: Entered, owns: Owns = lambda source: False) -> None:
    """Read the files of drags entering view_class, and refuse the app's own drags out.

    A drag out that comes back over a window is the page's to handle. WebKit would take the
    promise and write it somewhere of its own.
    """
    with _lock:
        if view_class in _watched:
            return
        _watched.add(view_class)

    if "draggingEntered_" in vars(view_class):
        logger.warning("%s has a draggingEntered of its own and isn't watched for dragged files",
                       view_class.__name__)
        return

    def dragging_entered(self: Any, info: Any) -> int:
        if _owned(owns, info):
            return AppKit.NSDragOperationNone
        try:
            entered(self, info.draggingSequenceNumber(), file_paths(info.draggingPasteboard()))
        except Exception:
            logger.exception("couldn't read the dragged files")
        return objc.super(view_class, self).draggingEntered_(info)

    def dragging_updated(self: Any, info: Any) -> int:
        if _owned(owns, info):
            return AppKit.NSDragOperationNone
        return objc.super(view_class, self).draggingUpdated_(info)

    def prepare_for_drag(self: Any, info: Any) -> bool:
        if _owned(owns, info):
            return False
        return objc.super(view_class, self).prepareForDragOperation_(info)

    methods = [objc.selector(dragging_entered, selector=b"draggingEntered:", signature=b"Q@:@")]
    if "draggingUpdated_" not in vars(view_class):
        methods.append(objc.selector(dragging_updated, selector=b"draggingUpdated:", signature=b"Q@:@"))
    if "prepareForDragOperation_" not in vars(view_class):
        methods.append(objc.selector(prepare_for_drag, selector=b"prepareForDragOperation:", signature=b"Z@:@"))
    objc.classAddMethods(view_class, methods)


def _owned(owns: Owns, info: Any) -> bool:
    try:
        return bool(owns(info.draggingSource()))
    except Exception:
        logger.exception("couldn't tell whose drag it is")
        return False


def file_paths(pasteboard: Any) -> list[Path]:
    options = {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}
    paths = []
    for url in pasteboard.readObjectsForClasses_options_([AppKit.NSURL], options) or []:
        # Finder drags file reference URLs
        url = url.filePathURL()
        if url is not None and url.path():
            paths.append(Path(url.path()))
    return paths
