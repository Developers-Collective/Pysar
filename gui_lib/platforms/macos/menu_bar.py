import logging
import threading
from collections.abc import Sequence
from typing import Any

import AppKit
import Foundation
import objc
from PyObjCTools import AppHelper

from ...windowing.menus import MenuItem

logger = logging.getLogger(__name__)

ROLES = {
    "about": "about:",
    "hide": "hide:",
    "hideOthers": "hideOtherApplications:",
    "showAll": "unhideAllApplications:",
    "quit": "terminate:",
    "undo": "undo:",
    "redo": "redo:",
    "cut": "cut:",
    "copy": "copy:",
    "paste": "paste:",
    "selectAll": "selectAll:",
    "close": "performClose:",
    "minimize": "performMiniaturize:",
    "zoom": "performZoom:",
    "front": "arrangeInFront:",
    "fullscreen": "toggleFullScreen:",
}

ROLE_TITLES = {"undo": "Undo", "redo": "Redo", "cut": "Cut", "copy": "Copy", "paste": "Paste",
               "selectAll": "Select All"}

MENU_ROLES = {"services": "setServicesMenu_", "window": "setWindowsMenu_", "help": "setHelpMenu_"}
MODIFIERS = {
    "cmdorctrl": AppKit.NSEventModifierFlagCommand,
    "cmd": AppKit.NSEventModifierFlagCommand,
    "shift": AppKit.NSEventModifierFlagShift,
    "alt": AppKit.NSEventModifierFlagOption,
    "option": AppKit.NSEventModifierFlagOption,
    "ctrl": AppKit.NSEventModifierFlagControl,
}

_items: tuple[MenuItem, ...] = ()
_menu: Any = None
_about: dict[str, str] = {}
_observer: Any = None


class GuiLibMenuTarget(AppKit.NSObject):
    def run_(self, sender: Any) -> None:
        item = sender.representedObject()
        if not isinstance(item, MenuItem) or item.action is None:
            return
        if item.role in ROLES and _roles_turn(item):
            AppKit.NSApp.sendAction_to_from_(ROLES[item.role], None, sender)
            return

        threading.Thread(target=_run, args=(item,), name="gui-menu", daemon=True).start()

    def about_(self, sender: Any) -> None:
        AppKit.NSApp.orderFrontStandardAboutPanelWithOptions_(_about)

    @objc.typedSelector(b"Z@:")
    def worksWhenModal(self) -> bool:
        return True

    @objc.typedSelector(b"Z@:@")
    def validateMenuItem_(self, menu_item: Any) -> bool:
        item = menu_item.representedObject()
        if not isinstance(item, MenuItem):
            return True
        if item.role in ROLES and item.action is not None:
            roles = _roles_turn(item)
            menu_item.setTitle_(ROLE_TITLES.get(item.role, item.title) if roles else item.title)
            if roles:
                return _taken(ROLES[item.role], menu_item)
        if AppKit.NSApp.modalWindow() is not None:
            return False
        return item.is_enabled()


_target = GuiLibMenuTarget.alloc().init()


def install_menu(items: Sequence[MenuItem], *, name: str, version: str | None) -> None:
    items = tuple(items)
    AppHelper.callAfter(_install, items, name, version)


def _install(items: tuple[MenuItem, ...], name: str, version: str | None) -> None:
    global _items, _about
    _about = {"ApplicationName": name, **({"ApplicationVersion": version, "Version": ""} if version else {})}
    _items = items
    _watch_key_windows()
    _show()


def _show() -> None:
    global _menu
    try:
        menu = AppKit.NSMenu.alloc().initWithTitle_("")
        for item in _items:
            menu.addItem_(_menu_item(item))
        _menu = menu
        AppKit.NSApp.setMainMenu_(menu)
    except Exception:
        logger.exception("couldn't make the menu bar")


def _menu_item(item: MenuItem) -> Any:
    if item.separator:
        return AppKit.NSMenuItem.separatorItem()

    action = target = None
    if item.action is not None:
        action, target = "run:", _target
    elif item.items is None and item.role is not None:
        action = ROLES.get(item.role)
        if action is None:
            logger.warning("there's no menu item with the role %r", item.role)
        elif item.role == "about":
            target = _target

    key, modifiers = _key_equivalent(item.shortcut)
    menu_item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(item.title, action, key)
    if key:
        menu_item.setKeyEquivalentModifierMask_(modifiers)
    menu_item.setTarget_(target)
    menu_item.setRepresentedObject_(item)

    if item.items is not None:
        submenu = AppKit.NSMenu.alloc().initWithTitle_(item.title)
        for child in item.items:
            submenu.addItem_(_menu_item(child))
        menu_item.setSubmenu_(submenu)
        if item.role in MENU_ROLES:
            getattr(AppKit.NSApp, MENU_ROLES[item.role])(submenu)
    return menu_item


def _key_equivalent(shortcut: str | None) -> tuple[str, int]:
    if not shortcut:
        return "", 0
    *modifiers, key = shortcut.split("+")
    mask = 0
    for modifier in modifiers:
        mask |= MODIFIERS[modifier.lower()]
    return key.lower(), mask


def _watch_key_windows() -> None:
    global _observer
    if _observer is not None:
        return

    # After pywebview's own handler, which may have set its menu meanwhile
    def became_key(notification: Any) -> None:
        AppHelper.callAfter(_restore)

    _observer = Foundation.NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
        AppKit.NSWindowDidBecomeKeyNotification, None, Foundation.NSOperationQueue.mainQueue(), became_key)


def _restore() -> None:
    if _menu is not None and (AppKit.NSApp.mainMenu() != _menu or _menu.numberOfItems() != len(_items)):
        logger.debug("the menu bar was replaced, putting it back")
        _show()


def _roles_turn(item: MenuItem) -> bool:
    from .backend import backend_of

    window = AppKit.NSApp.keyWindow()
    if window is None:
        return False
    backend = backend_of(window)
    if backend is None:
        return True
    owner = backend._owner
    return owner.text_focused or (item.role == "copy" and owner.text_selected)


def _taken(action: str, sender: Any) -> bool:
    return AppKit.NSApp.targetForAction_to_from_(action, None, sender) is not None


def _run(item: MenuItem) -> None:
    try:
        item.action()
    except Exception:
        logger.exception("the menu item %r failed", item.title)
