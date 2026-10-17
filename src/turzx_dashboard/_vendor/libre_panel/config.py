# SPDX-License-Identifier: GPL-3.0-or-later
"""The configuration fields used by the extracted Libre Panel USB drivers.

Adapted from Libre Panel's dataclasses; GUI, sensors and other devices omitted.
"""

from dataclasses import dataclass, field


@dataclass
class VideoConfig:
    fps: int = 30
    device_fps: int = 40
    crf: int = 25
    preset: str = "superfast"
    keyframe_s: float = 10.0
    maxrate: str = "2M"
    ffmpeg: str = ""
    local_clip: str = ""


@dataclass
class DeviceConfig:
    brightness: int = 60
    video: VideoConfig = field(default_factory=VideoConfig)
