# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from Libre Panel: imports relocated; see THIRD_PARTY.md.
"""Timing guards while Libre Panel drives a panel (SPUR II's).

At 50 fps a frame has 20 ms. SPUR II measured what keeps that steady when the
PC is busy (a game starting, the boot): Windows' timer at 1 ms (the 95th
percentile of the frame interval went from 24.7 to 18.2 ms), the garbage
collector kept from walking everything that lives as long as the app (its
full collections paused frames for up to 34.5 ms), and the threads that feed
the panel at above normal priority. All of it is done by the main loop while
it runs and undone after; importing this module changes nothing.
"""

from __future__ import annotations

import gc
import logging
import sys
import threading
from typing import Any

log = logging.getLogger(__name__)

GC_THRESHOLDS = (50000, 200, 200)  # SPUR II's: young collections rarely, the rest seldom
THREAD_PRIORITY_ABOVE_NORMAL = 1

_lock = threading.Lock()
_settled = 0  # main loops that froze the collector (it is frozen while any runs)
_thresholds: tuple[int, ...] | None = None


def _winmm() -> Any:
    import ctypes

    return ctypes.WinDLL("winmm")


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentThread.restype = wintypes.HANDLE
    kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    kernel32.SetThreadPriority.restype = wintypes.BOOL
    return kernel32


class Guards:
    """The guards for one run of the main loop: :meth:`start` as it begins,
    :meth:`settled` once its first frame is out, :meth:`stop` at its end."""

    def __init__(
        self, platform: str = sys.platform, winmm: Any = None, collector: Any = gc
    ) -> None:
        self.platform, self.collector = platform, collector
        self._winmm = winmm
        self._period = False
        self._frozen = False

    def start(self) -> None:
        """Windows' timer at 1 ms: sleeps between frames end on time."""
        if self.platform != "win32" or self._period:
            return
        try:
            winmm = self._winmm or _winmm()
            self._period = winmm.timeBeginPeriod(1) == 0  # TIMERR_NOERROR
        except (OSError, AttributeError) as exc:
            log.debug("timer resolution not raised: %s", exc)

    def settled(self) -> None:
        """What lives after start-up (themes, fonts, pieces) is left out of
        garbage collection from now on, and collections come less often."""
        global _settled, _thresholds
        if self._frozen:
            return
        with _lock:
            if _settled == 0:
                _thresholds = self.collector.get_threshold()
                self.collector.freeze()
                self.collector.set_threshold(*GC_THRESHOLDS)
            _settled += 1
        self._frozen = True

    def stop(self) -> None:
        global _settled
        if self._period:
            self._period = False
            try:
                (self._winmm or _winmm()).timeEndPeriod(1)
            except (OSError, AttributeError) as exc:
                log.debug("timer resolution not restored: %s", exc)
        if self._frozen:
            self._frozen = False
            with _lock:
                _settled -= 1
                if _settled == 0:  # the last main loop: as before
                    self.collector.unfreeze()
                    if _thresholds is not None:
                        self.collector.set_threshold(*_thresholds)


def raise_priority(platform: str = sys.platform, kernel32: Any = None) -> bool:
    """Above normal priority for the calling thread, on Windows: the threads
    that feed the panel must not starve when the PC is busy. False elsewhere
    (or when Windows refuses)."""
    if platform != "win32":
        return False
    try:
        kernel32 = kernel32 or _kernel32()
        handle = kernel32.GetCurrentThread()
        return bool(kernel32.SetThreadPriority(handle, THREAD_PRIORITY_ABOVE_NORMAL))
    except (OSError, AttributeError) as exc:
        log.debug("thread priority not raised: %s", exc)
        return False
