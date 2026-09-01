from __future__ import annotations

import threading

from core.errors import PipelineCancelled


class CancellationToken:
    """Thread-safe cooperative cancellation shared by CLI, GUI, and clients."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise PipelineCancelled("The audiobook run was canceled by the user.")
