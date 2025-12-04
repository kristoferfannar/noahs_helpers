from __future__ import annotations
import sys

from core.utils import eprint


class TimeoutException(Exception):
    pass


TIMEOUT_ERROR_CODE = 124


class Timer:
    def __init__(self, timeout_sec: float, consumed: float = 0) -> None:
        self._timeout_sec = timeout_sec
        self._consumed_sec = consumed

    def copy(self) -> Timer:
        return Timer(self._timeout_sec, self._consumed_sec)

    def get_time(self) -> float:
        return self._consumed_sec

    def add_time(self, time: float):
        self._consumed_sec += time

        if self._consumed_sec > self._timeout_sec:
            eprint(
                f"Player timed out: consumed={self._consumed_sec}, timeout={self._timeout_sec}"
            )
            sys.exit(TIMEOUT_ERROR_CODE)
