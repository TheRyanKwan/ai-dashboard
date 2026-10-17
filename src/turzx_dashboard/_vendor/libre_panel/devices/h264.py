# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from Libre Panel: imports relocated; see THIRD_PARTY.md.
"""H.264 for panels with a hardware video decoder: ffmpeg encodes, pictures come out one by one.

The encoder settings were measured on the 9.2" TURZX panel by the SPUR II
project (docs/protocol/turzx-usb.md, "Video layer"):

- Constrained Baseline, no B-frames, and **one slice per picture**
  (``sliced-threads=0``): the panel acknowledges multi-slice pictures but
  shows nothing.
- A keyframe every few seconds (3-10 s). Every keyframe re-quantises static
  areas, so at 1 s and below flat backgrounds visibly pulse.
  ``repeat-headers=1`` lets the decoder join at any keyframe.
- ffmpeg rotates into the panel's framebuffer (``transpose``); the stream is
  bit-identical to rotating in Python and costs the render loop nothing.

:class:`PictureSplitter` cuts ffmpeg's output into pictures by their NAL
units. That works however the pipe splits the bytes (Windows pipes deliver
small pieces).
"""

from __future__ import annotations

import collections
import logging
import queue
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil

from turzx_dashboard._vendor.libre_panel import bundled
from turzx_dashboard._vendor.libre_panel.devices.base import DeviceError
from turzx_dashboard._vendor.libre_panel.i18n import t

log = logging.getLogger(__name__)

X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")

NAL_SLICE, NAL_IDR = 1, 5
# NAL types that open a new picture when they follow a slice (H.264 7.4.1.2.3):
# SEI, SPS, PPS, access unit delimiter and the reserved 14-18.
_OPENS_PICTURE = {6, 7, 8, 9, 14, 15, 16, 17, 18}

# Windows: a console program started from a program without a console (the
# tray app) gets a black window of its own unless told not to.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class EncoderSettings:
    fps: int = 50
    crf: int = 25
    preset: str = "superfast"
    keyframe_s: float = 10.0
    maxrate: str = "2M"
    threads: int = 1  # frame threads add a frame of latency each


# How ffmpeg turns a theme frame into the panel's framebuffer, per rotation
# (the same turns as turzx_usb.to_native).
ROTATION_FILTERS = {
    None: None,
    270: "transpose=1",  # clockwise, like PIL's ROTATE_270
    180: "hflip,vflip",
}


def find_ffmpeg(configured: str = "") -> str:
    """The ffmpeg program: the configured path, the one a download carries (its ``tools``
    folder), PATH, or next to Libre Panel."""
    exe = bundled.program_name("ffmpeg")
    if configured:
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        raise DeviceError(t("ffmpeg not found at {path} (display.ffmpeg in dashboard.local.yaml)", path=path))
    own = bundled.find(exe)  # first: the build the download was tested with
    if own:
        return own
    found = shutil.which("ffmpeg")
    if found:
        return found
    beside = Path(sys.executable).resolve().parent / exe
    if beside.is_file():
        return str(beside)
    raise DeviceError(
        t(
            "video needs FFmpeg with libx264; install it on PATH, use --ffmpeg, "
            "or set display.ffmpeg in dashboard.local.yaml"
        )
    )


def ffmpeg_command(
    ffmpeg: str, size: tuple[int, int], rotate: int | None, settings: EncoderSettings
) -> list[str]:
    """ffmpeg reading raw RGB frames of ``size`` on stdin, writing H.264 (Annex B) to stdout."""
    width, height = size
    keyint = max(1, round(settings.fps * settings.keyframe_s))
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-framerate",
        str(settings.fps),
        "-i",
        "-",
        "-an",
    ]
    video_filter = ROTATION_FILTERS[rotate]
    if video_filter:
        command += ["-vf", video_filter]
    return command + [
        "-c:v",
        "libx264",
        "-profile:v",
        "baseline",
        "-preset",
        settings.preset,
        "-tune",
        "zerolatency",
        "-threads",
        str(settings.threads),
        "-crf",
        str(settings.crf),
        "-maxrate",
        settings.maxrate,
        "-bufsize",
        settings.maxrate,
        "-x264-params",
        # zerolatency turns slice threads on; the panel needs one slice per picture
        f"sliced-threads=0:bframes=0:scenecut=0:keyint={keyint}:min-keyint={keyint}"
        ":ipratio=2.0:repeat-headers=1",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "h264",
        "-",
    ]


def nal_units(data: bytes) -> list[tuple[int, int]]:
    """(offset, type) of every NAL unit in an Annex B stream."""
    found, pos = [], 0
    while True:
        i = data.find(b"\x00\x00\x01", pos)
        if i < 0 or i + 3 >= len(data):
            return found
        found.append((i + 3, data[i + 3] & 0x1F))
        pos = i + 3


def slice_count(data: bytes) -> int:
    return sum(1 for _, kind in nal_units(data) if kind in (NAL_SLICE, NAL_IDR))


class PictureSplitter:
    """Cuts an Annex B stream into pictures: parameter sets and SEI, then the slice.

    A picture is known to be complete when the next one begins, so each one
    is handed on when the next arrives (one frame later: 20 ms at 50 fps);
    :meth:`flush` returns the last one at the end of the stream.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0  # start codes before this offset are handled
        self._has_slice = False
        self.multi_slice = False

    def feed(self, data: bytes) -> list[bytes]:
        buf = self._buf
        buf += data
        pictures = []
        pos = self._pos
        while True:
            i = buf.find(b"\x00\x00\x01", pos)
            if i < 0:
                pos = max(pos, len(buf) - 2)  # a start code may straddle the next piece
                break
            if i + 4 >= len(buf):  # need the NAL header and the first slice header byte
                pos = i
                break
            kind = buf[i + 3] & 0x1F
            opens = False
            if kind in (NAL_SLICE, NAL_IDR):
                first_in_picture = bool(buf[i + 4] & 0x80)  # first_mb_in_slice == 0
                opens = self._has_slice and first_in_picture
                if self._has_slice and not first_in_picture:
                    self.multi_slice = True
            elif kind in _OPENS_PICTURE:
                opens = self._has_slice
            if opens:
                cut = i - 1 if i > 0 and buf[i - 1] == 0 else i  # 4-byte start code
                pictures.append(bytes(buf[:cut]))
                del buf[:cut]
                i -= cut
                self._has_slice = False
            if kind in (NAL_SLICE, NAL_IDR):
                self._has_slice = True
            pos = i + 3
        self._pos = pos
        return pictures

    def flush(self) -> list[bytes]:
        """The last picture, at the end of the stream."""
        rest = bytes(self._buf) if self._has_slice else b""
        self._buf.clear()
        self._pos, self._has_slice = 0, False
        return [rest] if rest else []


class EncoderStopped(DeviceError):
    """ffmpeg ended or stopped taking frames."""


class Encoder:
    """One ffmpeg process: raw frames in, pictures out.

    A writer thread feeds ffmpeg, so a stalled ffmpeg cannot block the render
    loop for longer than the ``write`` timeout. A reader thread cuts the output
    into pictures and hands each to ``deliver``, which may block: that is the
    back-pressure from the panel, passed through the pipe to the render loop.
    """

    def __init__(
        self,
        command: list[str],
        size: tuple[int, int],
        deliver: Callable[[bytes], None],
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self.size = size
        self.command = command
        self._deliver = deliver
        self._frames: queue.Queue[bytes | None] = queue.Queue(maxsize=1)
        self._closing = threading.Event()
        self._done = threading.Event()  # ffmpeg's output ended
        self.errors: collections.deque[str] = collections.deque(maxlen=10)
        self.splitter = PictureSplitter()
        self.pictures = 0
        try:
            self.proc = popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=NO_WINDOW,
            )
        except OSError as exc:
            raise DeviceError(t("cannot start ffmpeg: {error}", error=exc)) from exc
        self._threads = [
            threading.Thread(target=target, name=f"ffmpeg-{name}", daemon=True)
            for name, target in (
                ("writer", self._write_loop),
                ("reader", self._read_loop),
                ("errors", self._error_loop),
            )
        ]
        for thread in self._threads:
            thread.start()

    @property
    def alive(self) -> bool:
        return not self._done.is_set() and self.proc.poll() is None

    def reason(self) -> str:
        """Why ffmpeg stopped, as far as it said."""
        if self.errors:
            return self.errors[-1]
        code = self.proc.poll()
        return f"exit code {code}" if code is not None else "no output"

    def write(self, frame: bytes, timeout: float = 2.0) -> None:
        if not self.alive:
            raise EncoderStopped(f"ffmpeg stopped ({self.reason()})")
        try:
            self._frames.put(frame, timeout=timeout)
        except queue.Full:
            raise EncoderStopped(f"ffmpeg takes no frames for {timeout:.0f} s") from None

    def _write_loop(self) -> None:
        stdin = self.proc.stdin
        try:
            while True:
                frame = self._frames.get()
                if frame is None or self._closing.is_set():
                    break
                view = memoryview(frame)
                while view:  # an unbuffered pipe may take less than asked
                    view = view[stdin.write(view) :]
        except (OSError, ValueError):  # ffmpeg is gone; the reader notices the end
            pass
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    def _read_loop(self) -> None:
        stdout = self.proc.stdout
        try:
            while True:
                data = stdout.read(65536)
                if not data:
                    break
                for picture in self.splitter.feed(data):
                    self._hand_on(picture)
            if not self._closing.is_set():
                for picture in self.splitter.flush():
                    self._hand_on(picture)
        except (OSError, ValueError):
            pass
        finally:
            self._done.set()

    def _hand_on(self, picture: bytes) -> None:
        if self._closing.is_set():
            return
        self.pictures += 1
        self._deliver(picture)

    def _error_loop(self) -> None:
        try:
            for line in self.proc.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self.errors.append(text)
                    log.warning("ffmpeg: %s", text)
        except (OSError, ValueError):
            pass

    def close(self, timeout: float = 3.0) -> None:
        """Stop ffmpeg; pictures still in the pipe are dropped."""
        self._closing.set()
        try:
            self._frames.put_nowait(None)
        except queue.Full:
            try:  # make room for the stop sign; the frame is not needed any more
                self._frames.get_nowait()
                self._frames.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass
        children = self._children()  # before the parent ends: they are found through it
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout)
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                pass
        for thread in self._threads:
            thread.join(timeout)
        if any(thread.is_alive() for thread in self._threads):
            return  # a thread still reads a pipe; closing it under the thread can block
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    def _children(self) -> list[psutil.Process]:
        """What ffmpeg started. ffmpeg from a package manager (Chocolatey, scoop) is a
        shim that starts the real one: ending the shim alone leaves that running, and
        a frozen one keeps the pipes open for good."""
        try:
            return psutil.Process(self.proc.pid).children(recursive=True)
        except (psutil.Error, AttributeError, TypeError, ValueError):
            return []
