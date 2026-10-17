# SPDX-License-Identifier: GPL-3.0-or-later
"""Atomic simulator captures and the real USB video/still outputs."""

import os
import tempfile
from pathlib import Path

from ._vendor.libre_panel.config import DeviceConfig, VideoConfig
from ._vendor.libre_panel.devices.models import find_model


def save_png(frame, path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".png", delete=False) as stream:
            temporary = Path(stream.name)
            frame.save(stream, format="PNG")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def create_display(settings, still=False):
    from ._vendor.libre_panel.devices.base import DeviceError
    try:
        import usb.core  # noqa: F401
        from Crypto.Cipher import DES  # noqa: F401
    except ImportError:
        raise DeviceError("USB output needs: pip install 'turzx-dashboard[usb]'") from None
    if still:
        from ._vendor.libre_panel.devices.turzx_usb import TurzxUsbDisplay as Driver
    else:
        from ._vendor.libre_panel.devices.turzx_video import TurzxVideoDisplay as Driver
    config = DeviceConfig(brightness=settings.brightness, video=VideoConfig(
        fps=settings.fps, device_fps=min(60, settings.fps + 10),
        ffmpeg=settings.ffmpeg, local_clip=settings.clip))
    return Driver(config, find_model("turing-9.2-usb"))
