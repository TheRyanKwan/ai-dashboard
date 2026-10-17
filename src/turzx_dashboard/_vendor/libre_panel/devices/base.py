# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from Libre Panel: retained the display interface; see THIRD_PARTY.md.
"""Display driver interface.

A driver only moves pixels to hardware. It knows nothing about themes or
sensors, which keeps adding a new panel family (TURZX revisions, other
vendors) down to one small class registered under ``libre_panel.devices``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PIL import Image

from turzx_dashboard._vendor.libre_panel.config import DeviceConfig
from turzx_dashboard._vendor.libre_panel.devices.models import PanelModel


class DeviceError(RuntimeError):
    pass


class FrameError(DeviceError):
    """This frame cannot be shown (e.g. too large); the connection is fine."""


@dataclass
class Counters:
    """What the drivers count while the panel runs, for the editor's diagnostics.

    The main loop gives every display the same one (``display.counters``), so the
    numbers go on when a display is made again after a replug. A driver adds to the
    counters that apply to it; each is written by one thread, so nothing is locked
    and counting costs a few additions per frame.
    """

    bytes: int = 0  # sent to the panel
    blocks: int = 0  # video: pictures handed to the panel's decoder
    waits: int = 0  # video: times the sender waited for room in the panel's queue
    drops: int = 0  # video: pictures thrown away unsent (the stream was replaced)
    max_depth: int = 0  # video: the deepest queue the panel reported
    usb_errors: int = 0  # errors on the panel's link that made the driver reconnect
    panel_restarts: int = 0  # video: restarts of the panel to clear a hung decoder
    ffmpeg_restarts: int = 0  # video: times ffmpeg stopped by itself and was started again


class Display:
    name = "base"
    # A streaming display (a video encoder) wants every frame at ``stream_fps``,
    # changed or not; others get only frames that changed, at the configured fps.
    streaming = False
    stream_fps = 0
    # a slow link (the USB serial panels, pixels over a serial port): the main loop keeps the
    # motion calm and sends the parts that changed one by one (app.calm, changed_regions)
    slow = False
    # True once open() brought the panel back after the driver restarted it
    # (a hung video decoder): the main loop tells the services, unlike a replug.
    restarted = False
    # The panel this is, once it is open and the driver knows (a panel found by its USB ids
    # learns its model when it connects); None before, and for drivers that do not say.
    model: PanelModel | None = None

    def __init__(self, config: DeviceConfig) -> None:
        self.config = config
        # Replaced by the main loop's, shared by every display it makes (and by the
        # displays a wrapping one makes inside: ``auto``, ``turzx``).
        self.counters = Counters()

    def open(self) -> None:
        """Connect and initialise the panel."""

    def close(self) -> None:
        """Release the device; must be safe to call twice."""

    def set_brightness(self, percent: int) -> None:
        """0-100. Drivers without brightness control ignore it."""

    def describe(self) -> str:
        """What frames go to, for status displays (tray, editor)."""
        return self.name

    def show(self, frame: Image.Image, region: tuple[int, int, int, int] | None = None) -> None:
        """Push ``frame`` (RGB, theme size). ``region`` is the changed box
        (x0, y0, x1, y1); drivers that support partial updates send only that."""
        raise NotImplementedError

    def __enter__(self) -> Display:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

