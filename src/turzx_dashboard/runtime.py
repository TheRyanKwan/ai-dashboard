# SPDX-License-Identifier: GPL-3.0-or-later
"""Paced playback, with retries after unplugging or decoder recovery."""

import logging
import signal
import threading
import time

from ._vendor.libre_panel.devices.base import DeviceError
from ._vendor.libre_panel.timing import Guards
from .data import DashboardData, demo_codex
from .output import create_display, save_png
from .render import DashboardRenderer, physical_frame

log = logging.getLogger(__name__)


def run(settings, *, simulate=False, output="output/frame.png", duration=None,
        state=None, still=False, stop=None, display=None, source=None, frame_sink=None):
    stop = stop or threading.Event()
    source = source or DashboardData(settings.dashboard)
    renderer = DashboardRenderer(settings.reduced_motion)
    if not simulate:
        display = display or create_display(settings, still)
    source.start()
    guards = Guards()
    connected = False
    retry_at = 0
    frames = 0
    started = time.monotonic()
    next_frame = started
    next_capture = started
    last_status = None
    previous_handlers = {}

    def request_stop(*_):
        stop.set()

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, request_stop)
    try:
        guards.start()
        while not stop.is_set():
            now = time.monotonic()
            elapsed = now - started
            if duration is not None and elapsed >= duration:
                break
            snapshot = source.snapshot()
            snapshot["demo"] = settings.dashboard["DEMO"]
            if state is not None:
                snapshot["codex"] = (demo_codex(state) if snapshot["demo"] else
                                     dict(snapshot["codex"], state=state))
            elif snapshot["demo"]:
                snapshot["codex"] = demo_codex(("idle", "working", "done")[int(elapsed / 6) % 3])
            status = tuple(snapshot.get(k, "") for k in
                           ("account_status", "daily_status", "chart_status", "codex_status"))
            if status != last_status:
                if any(status):
                    log.warning("Data: %s", "; ".join(dict.fromkeys(s for s in status if s)))
                else:
                    log.info("Dashboard data ready%s", " (demo)" if snapshot["demo"] else "")
                last_status = status
            if simulate:
                if frame_sink is not None or now >= next_capture:
                    frame = renderer.frame(snapshot, elapsed)
                    if frame_sink is not None:
                        frame_sink(frame)
                        frames += 1
                if now >= next_capture:
                    save_png(frame, output)
                    next_capture = now + .25  # do not encode PNGs at the video frame rate
                    if frame_sink is None:
                        frames += 1
            elif now >= retry_at:
                try:
                    if not connected:
                        display.open()
                        display.set_brightness(settings.brightness)
                        connected = True
                        log.info("Connected: %s", display.describe())
                    frame = renderer.frame(snapshot, elapsed)
                    display.show(physical_frame(frame, settings.y_offset, settings.rotate))
                    frames += 1
                except DeviceError as exc:
                    log.warning("Display: %s; retrying in 3 seconds", exc)
                    display.close()
                    connected = False
                    retry_at = time.monotonic() + 3
            # Discard missed deadlines instead of accumulating a queue of stale frames.
            fps = min(settings.fps, 8) if still else settings.fps
            next_frame = max(next_frame + 1 / fps, time.monotonic())
            delay = next_frame - time.monotonic()
            if duration is not None:
                delay = min(delay, max(0, duration - (time.monotonic() - started)))
            stop.wait(max(0, delay))
    finally:
        try:
            if display is not None:
                display.close()
        finally:
            source.close()
            guards.stop()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
    log.info("Stopped after %d frames in %.1f seconds", frames, time.monotonic() - started)
    return frames
