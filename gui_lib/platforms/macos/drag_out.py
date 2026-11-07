import logging
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import AppKit
import Foundation
import objc
from PyObjCTools import AppHelper

from ..drag_out import (
    DragOutSession,
    DropFolderError,
    PendingDrop,
    PendingDrops,
    Point,
    drop_folder,
)

if TYPE_CHECKING:
    from ...windowing.drag_out import DragOut
    from ...windowing.window import Window

logger = logging.getLogger(__name__)

FILE_TYPE = "public.data"
PADDING_X = 10.0
PADDING_Y = 5.0
FONT_SIZE = 13.0
RADIUS = 7.0
# How long an ended drag keeps its objects for a promise that hasn't come yet
LATE_PROMISE_SECONDS = 60.0

# AppKit keeps the promise's delegate weakly, so the sessions are kept here
_live: list["MacDragOut"] = []


class MacDragOut(DragOutSession):
    def __init__(self, owner: "Window", drag: "DragOut", web_view: Any, drops: PendingDrops,
                 front: Callable[[], None]) -> None:
        super().__init__(owner, drag)
        self.web_view = web_view
        self._drops = drops
        self._front = front
        self.drop: PendingDrop | None = None
        self.ended_at: float | None = None
        self.retained: tuple[Any, ...] = ()

    def page_point(self, screen: Any) -> Point | None:
        return page_point(self.web_view, screen)

    def finished(self, screen: Any, operation: int) -> None:
        self.ended_at = time.monotonic()
        self.ended(self.page_point(screen), operation != AppKit.NSDragOperationNone)
        release_web_view(self.web_view, screen)

    def promised(self, url: Any, handler: Callable[[Any], None]) -> None:
        try:
            path = url.path() if url is not None and url.isFileURL() else None
            if not path:
                raise DropFolderError(f"drop URL {url!r} isn't a file URL")
            folder = drop_folder(Path(path).parent)
        except DropFolderError as error:
            logger.warning("refused a drop: %s", error)
            handler(_error(Foundation.NSFileWriteNoPermissionError))
            return

        def answer(ok: bool) -> None:
            handler(None if ok else _error(Foundation.NSUserCancelledError))

        self.drop = drop = PendingDrop(folder, answer, AppHelper.callAfter)
        self._drops.add(drop)
        self._front()
        self.folder_dropped(folder, drop)

    def kept(self, now: float) -> bool:
        if self.ended_at is None or (self.drop is not None and not self.drop.done):
            return True
        return self.drop is None and now - self.ended_at < LATE_PROMISE_SECONDS


def _error(code: int) -> Any:
    return Foundation.NSError.errorWithDomain_code_userInfo_(Foundation.NSCocoaErrorDomain, code, None)


def _protocols(*names: str) -> list[Any]:
    found = []
    for name in names:
        try:
            found.append(objc.protocolNamed(name))
        except objc.ProtocolError:
            logger.debug("AppKit has no protocol %s", name)
    return found


class GuiLibDragOutSource(Foundation.NSObject, protocols=_protocols("NSDraggingSource")):
    def initWithSession_(self, session: MacDragOut) -> Any:
        made = objc.super(GuiLibDragOutSource, self).init()
        if made is not None:
            made.session = session
        return made

    def draggingSession_sourceOperationMaskForDraggingContext_(self, dragging: Any, context: int) -> int:
        return AppKit.NSDragOperationCopy

    def draggingSession_movedToPoint_(self, dragging: Any, point: Any) -> None:
        try:
            self.session.moved(self.session.page_point(point))
        except Exception:
            logger.exception("couldn't follow the drag")

    def draggingSession_endedAtPoint_operation_(self, dragging: Any, point: Any, operation: int) -> None:
        try:
            self.session.finished(point, operation)
        except Exception:
            logger.exception("couldn't end the drag")


class GuiLibFilePromiseDelegate(Foundation.NSObject, protocols=_protocols("NSFilePromiseProviderDelegate")):
    def initWithSession_(self, session: MacDragOut) -> Any:
        made = objc.super(GuiLibFilePromiseDelegate, self).init()
        if made is not None:
            made.session = session
        return made

    def filePromiseProvider_fileNameForType_(self, provider: Any, file_type: str) -> str:
        return self.session.name

    # PyObjC's metadata has no signature for the handler.
    # It reads the one AppKit's block carries
    def filePromiseProvider_writePromiseToURL_completionHandler_(self, provider: Any, url: Any,
                                                                  handler: Callable[[Any], None]) -> None:
        try:
            self.session.promised(url, handler)
        except Exception:
            logger.exception("couldn't take a drop")
            handler(_error(Foundation.NSFileWriteUnknownError))

    def operationQueueForFilePromiseProvider_(self, provider: Any) -> Any:
        # The handler above only hands the drop on, so the main thread is fine
        return Foundation.NSOperationQueue.mainQueue()


def owns(source: Any) -> bool:
    """Whether a drag is one of these"""
    return isinstance(source, GuiLibDragOutSource)


def begin(session: MacDragOut, ns_window: Any) -> bool:
    web_view = session.web_view
    if ns_window is None or web_view is None or not AppKit.NSEvent.pressedMouseButtons() & 1:
        return False

    # The page's drag gave AppKit no event of its own to start from
    in_window = ns_window.convertPointFromScreen_(AppKit.NSEvent.mouseLocation())
    event = AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
        AppKit.NSEventTypeLeftMouseDragged, in_window, 0, AppKit.NSProcessInfo.processInfo().systemUptime(),
        ns_window.windowNumber(), None, 0, 1, 1.0)

    source = GuiLibDragOutSource.alloc().initWithSession_(session)
    delegate = GuiLibFilePromiseDelegate.alloc().initWithSession_(session)
    provider = AppKit.NSFilePromiseProvider.alloc().initWithFileType_delegate_(FILE_TYPE, delegate)
    item = AppKit.NSDraggingItem.alloc().initWithPasteboardWriter_(provider)
    image = label_image(session.drag.label)
    size = image.size()
    at = web_view.convertPoint_fromView_(in_window, None)
    item.setDraggingFrame_contents_(AppKit.NSMakeRect(at.x - PADDING_X, at.y - size.height / 2, size.width, size.height),
                                    image)

    dragging = web_view.beginDraggingSessionWithItems_event_source_([item], event, source)
    if dragging is None:
        return False
    # The page flies its own ghost back
    dragging.setAnimatesToStartingPositionsOnCancelOrFail_(False)
    session.retained = (source, delegate, provider, dragging)
    now = time.monotonic()
    _live[:] = [each for each in _live if each.kept(now)]
    _live.append(session)
    return True


def label_image(label: str) -> Any:
    font = AppKit.NSFont.systemFontOfSize_weight_(FONT_SIZE, AppKit.NSFontWeightSemibold)
    attributes = {AppKit.NSFontAttributeName: font,
                  AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor()}
    text = Foundation.NSAttributedString.alloc().initWithString_attributes_(label, attributes)
    measured = text.size()
    size = AppKit.NSMakeSize(math.ceil(measured.width + 2 * PADDING_X), math.ceil(measured.height + 2 * PADDING_Y))

    def draw(rect: Any) -> bool:
        AppKit.NSColor.controlAccentColor().setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, RADIUS, RADIUS).fill()
        text.drawAtPoint_(AppKit.NSMakePoint(PADDING_X, PADDING_Y))
        return True

    return AppKit.NSImage.imageWithSize_flipped_drawingHandler_(size, False, draw)


def page_point(web_view: Any, screen: Any) -> Point | None:
    window = web_view.window() if web_view is not None else None
    if window is None:
        return None
    at = web_view.convertPoint_fromView_(window.convertPointFromScreen_(screen), None)
    y = at.y if web_view.isFlipped() else web_view.bounds().size.height - at.y
    return at.x, y


def release_web_view(web_view: Any, screen: Any) -> None:
    window = web_view.window() if web_view is not None else None
    if window is None:
        return
    try:
        event = AppKit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
            AppKit.NSEventTypeLeftMouseUp, window.convertPointFromScreen_(screen), 0,
            AppKit.NSProcessInfo.processInfo().systemUptime(), window.windowNumber(), None, 0, 1, 0.0)
        web_view.mouseUp_(event)
    except Exception:
        logger.debug("couldn't release the web view's mouse", exc_info=True)
