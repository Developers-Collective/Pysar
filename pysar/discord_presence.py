"""Best-effort Discord Rich Presence integration for Pysar."""

from __future__ import annotations

import threading
import time


DISCORD_APPLICATION_ID = "1536026395833270282"
_DEFAULT_SMALL_IMAGE = "project"
_RECONNECT_DELAY_SECONDS = 10.0
_SMALL_IMAGES = frozenset({
    "project",
    "stream",
    "wave_sound",
    "sequence",
    "bank",
    "group",
    "player",
    "wave_archive",
    "file",
})


class DiscordPresence:
    """Publish the current Pysar tab icon without affecting editor startup."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rpc = None
        self._small_image = _DEFAULT_SMALL_IMAGE
        self._published_image: str | None = None
        self._closed = False
        self._connecting = False
        self._last_connection_attempt = float("-inf")

    def start(self) -> None:
        """Connect in the background so a missing Discord client is harmless."""
        with self._lock:
            self._start_connection_locked()

    def _start_connection_locked(self) -> None:
        if self._closed or self._rpc is not None or self._connecting:
            return
        now = time.monotonic()
        if now - self._last_connection_attempt < _RECONNECT_DELAY_SECONDS:
            return
        self._connecting = True
        self._last_connection_attempt = now
        threading.Thread(
            target=self._connect,
            daemon=True,
            name="pysar-discord-rpc",
        ).start()

    def set_small_image(self, image: str | None) -> None:
        """Set the image key that corresponds to the active application tab."""
        image_key = self._normalise_image(image)
        with self._lock:
            self._small_image = image_key
            if self._rpc is None:
                self._start_connection_locked()
            else:
                self._publish_locked()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            rpc = self._rpc
            self._rpc = None
            self._published_image = None

        if rpc is None:
            return
        try:
            rpc.clear()
        except Exception:
            pass
        try:
            rpc.close()
        except Exception:
            pass

    def _connect(self) -> None:
        try:
            from pypresence import Presence

            rpc = Presence(DISCORD_APPLICATION_ID)
            rpc.connect()
        except Exception:
            with self._lock:
                self._connecting = False
            return

        with self._lock:
            self._connecting = False
            if self._closed:
                try:
                    rpc.close()
                except Exception:
                    pass
                return
            self._rpc = rpc
            self._publish_locked(force=True)

    def _publish_locked(self, *, force: bool = False) -> None:
        if self._rpc is None or (not force and self._published_image == self._small_image):
            return
        try:
            self._rpc.update(
                details="Pysar",
                state="Editing brsar archive",
                large_image="pysar",
                large_text="Pysar",
                small_image=self._small_image,
            )
        except Exception:
            try:
                self._rpc.close()
            except Exception:
                pass
            self._rpc = None
            self._published_image = None
            return
        self._published_image = self._small_image

    @staticmethod
    def _normalise_image(image: str | None) -> str:
        image_key = str(image or "").rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        return image_key if image_key in _SMALL_IMAGES else _DEFAULT_SMALL_IMAGE
