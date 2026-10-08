"""Graceful CLI shutdown without interrupting a database operation."""
from __future__ import annotations

import signal
import threading
from typing import Callable


def run_polling(runtime, *, once: bool = False, on_once: Callable | None = None) -> None:
    stopped = threading.Event()
    previous = {}

    def request_stop(signum, frame):
        stopped.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        while not stopped.is_set():
            result = runtime.poll_once(timeout_seconds=0 if once else 30)
            if once:
                if on_once is not None:
                    on_once(result)
                return
            stopped.wait(1)
    finally:
        try:
            runtime.close()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
