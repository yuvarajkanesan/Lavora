"""Cooperative cancellation. Long operations poll a token every few MiB, so Ctrl+C / a Cancel button always stops promptly."""
from __future__ import annotations

import contextlib
import signal
import threading


class CancelToken:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    def reset(self) -> None:
        self._ev.clear()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def __call__(self) -> bool:
        return self._ev.is_set()


def install_sigint(token: CancelToken) -> None:
    """First Ctrl+C asks the running operation to stop cleanly (progress is saved); a second one aborts immediately."""
    def handler(_sig, _frame) -> None:
        if token.cancelled:
            raise KeyboardInterrupt
        token.cancel()
        print("\nStopping after the current step... (press Ctrl+C again to abort at once)")

    with contextlib.suppress(ValueError):  # not the main thread (tests, GUI)
        signal.signal(signal.SIGINT, handler)
