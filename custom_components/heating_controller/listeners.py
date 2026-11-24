"""Update callbacks of the hub, its rooms and devices."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import callback


class Observable:
    """Calls its listeners whenever its state changed."""

    def __init__(self) -> None:
        self._listeners: list[Callable[[], None]] = []

    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Add a callback; the returned function removes it again."""
        self._listeners.append(update_callback)
        return lambda: self._listeners.remove(update_callback)

    @callback
    def _notify_listeners(self) -> None:
        # A copy: a listener may remove itself while being called.
        for update_callback in list(self._listeners):
            update_callback()
