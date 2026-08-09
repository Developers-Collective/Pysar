"""Native save-panel helpers that extend pywebview's existing dialogs."""

from __future__ import annotations

import sys
from pathlib import Path
from threading import Semaphore
from typing import Any


UNAVAILABLE = object()


def choose_macos_export_path(
    window: Any,
    default_filename: str,
    formats: tuple[tuple[str, str], ...],
    *,
    title: str,
) -> tuple[Path, str] | None | object:
    """Use pywebview's active Cocoa app for a native format-aware save panel.

    pywebview already supplies the native ``FileFilterChooser`` accessory for
    open panels. Reusing it here keeps the save panel in Pysar's own Cocoa
    process, rather than launching a second helper application.
    """
    if sys.platform != "darwin":
        return UNAVAILABLE

    try:
        from webview.platforms import cocoa
    except Exception:
        return UNAVAILABLE

    # ``Window.gui`` is the active pywebview backend *module*.  Confirm that
    # this window belongs to the Cocoa backend before using its private native
    # dialog primitives.
    if getattr(window, "gui", None) is not cocoa:
        return UNAVAILABLE

    filters = [(label, [suffix.lstrip(".")]) for label, suffix in formats]
    if not filters:
        raise ValueError("No export formats were provided")

    result: dict[str, Any] = {
        "accepted": False,
        "path": None,
        "filter": None,
        "error": None,
        "panel": None,
    }
    completed = Semaphore(0)

    def show_panel() -> None:
        try:
            browser = cocoa.BrowserView.instances.get(window.uid)
            if browser is None:
                raise RuntimeError("The Cocoa window is unavailable")

            panel = cocoa.AppKit.NSSavePanel.savePanel()
            panel.setTitle_(title)
            panel.setNameFieldStringValue_(default_filename)
            panel.setAllowedFileTypes_(filters[0][1])

            chooser = cocoa.BrowserView.FileFilterChooser.alloc().initWithFilter_(filters)
            chooser.selectItemAtIndex_(0)
            chooser.setFileDialog_(panel)
            panel.setAccessoryView_(chooser)
            result["panel"] = panel  # Keep the sheet and its callback alive.

            def complete(response: int) -> None:
                try:
                    if response == cocoa.AppKit.NSFileHandlingPanelOKButton:
                        result["accepted"] = True
                        selected_index = int(chooser.indexOfSelectedItem())
                        if not 0 <= selected_index < len(filters):
                            selected_index = 0
                        selected_url = panel.URL() if hasattr(panel, "URL") else None
                        selected_path = selected_url.path() if selected_url is not None else panel.filename()
                        if selected_path:
                            result["path"] = str(selected_path)
                        result["filter"] = filters[selected_index][0]
                except Exception as exc:
                    result["error"] = str(exc)
                finally:
                    result["panel"] = None
                    completed.release()

            # A sheet is owned by Pysar's actual Cocoa window. Unlike a new
            # application-modal loop, it keeps the existing browser event loop
            # responsive while users navigate folders in the save panel.
            panel.beginSheetModalForWindow_completionHandler_(browser.window, complete)
        except Exception as exc:
            result["error"] = str(exc)
            completed.release()

    cocoa.AppHelper.callAfter(show_panel)
    completed.acquire()
    if result["error"]:
        raise RuntimeError(f"Native export dialog failed: {result['error']}")
    if result["accepted"] and not result["path"]:
        raise RuntimeError("Native export dialog did not return a destination")
    if not result["path"]:
        return None
    return Path(result["path"]).expanduser(), str(result["filter"] or formats[0][0])
