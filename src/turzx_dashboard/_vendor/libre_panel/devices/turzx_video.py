# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from Libre Panel: imports relocated; see THIRD_PARTY.md.
"""Video mode for TURZX USB panels: frames go to the panel's H.264 decoder at up to 50 fps.

The panel has two layers: an H.264 stream (command 121) underneath and an
RGBA PNG (command 102) on top. The PNG path alone manages about 9 fps on
the 9.2", because the panel takes about 60 ms per full frame. The decoder has
no such limit. Libre Panel puts the whole frame into the video and keeps the
PNG layer transparent.

Everything here was measured on the 9.2" by the SPUR II project, which has
driven the panel this way around the clock at 50 fps
(docs/protocol/turzx-usb.md, "Video layer"):

- **Start:** ``110(name)`` clears the framebuffer (without it the screen
  stays black), ``111`` stops the local playback that 110 started, ``112``
  asks whether it still plays, then brightness, a transparent PNG layer, the
  player's rate (``15``) and the block size (``17``).
- **One picture per 121 block.** The panel's queue counts blocks, so a
  keyframe cut into three blocks looks like a queue of three.
- **Flow control:** ask for the queue depth (``122``) every second block;
  above 2, wait in 30 ms steps until it is at most 1. The queue is a ring of
  five blocks that silently overwrites unread ones, so flooding breaks the
  picture.
- **The player's rate** (``15``) is reported above the rate delivered: the
  player shows each picture strictly on its own clock and never catches up.
- **Stop:** ``123`` ends the stream, ``15 = 30`` gives the panel its power-on
  rate back (its own standby clip plays at the last rate set), and the last
  frame stays on the panel as a sharp PNG.
- **A hung decoder** takes each block only after about 750 ms and its queue
  stands high without moving. Detected as SPUR II does it (see
  :meth:`VideoSession.check_hang`). Nothing but a restart of the panel (11)
  or replugging clears it, so Libre Panel restarts the panel as SPUR II
  does: ``11``, wait until it leaves the bus (at most 20 s) and comes back
  (at most 90 s), 2 s more, then a full start with a new ffmpeg. At most
  three times in a row; after 300 s without a hang the count starts again.
  After that it asks to replug the panel.

This mode never sends commands that store anything on the panel: the start
sequence without 13 (save settings), 125 and 42 shows the video, and after a
power cycle the panel behaves exactly as before (measured on the 9.2").
The name sent with 110 stays in the panel's memory until it restarts.
"""

from __future__ import annotations

import logging
import queue
import statistics
import struct
import threading
import time
from collections import deque
from collections.abc import Callable

from PIL import Image

from turzx_dashboard._vendor.libre_panel import timing
from turzx_dashboard._vendor.libre_panel.devices.base import Counters, DeviceError, FrameError
from turzx_dashboard._vendor.libre_panel.devices.h264 import (
    Encoder,
    EncoderSettings,
    EncoderStopped,
    ffmpeg_command,
    find_ffmpeg,
)
from turzx_dashboard._vendor.libre_panel.devices.models import PanelModel, orientation_of
from turzx_dashboard._vendor.libre_panel.devices.turzx_usb import (
    CMD_BRIGHTNESS,
    CMD_FRAME_RATE,
    CMD_RESTART,
    CMD_STOP_STREAM,
    CMD_UPLOAD_PNG,
    TurzxUsbDisplay,
    UsbTransport,
    brightness_arg,
    encode_frame,
    encode_png_rgba,
    to_native,
)
from turzx_dashboard._vendor.libre_panel.i18n import t

log = logging.getLogger(__name__)

CMD_CHUNK_SIZE = 17
CMD_PLAY_LOCAL = 110  # clears the framebuffer; the name goes into the panel's settings in RAM
CMD_STOP_LOCAL = 111
CMD_LOCAL_PLAYING = 112
CMD_H264_BLOCK = 121
CMD_QUEUE_DEPTH = 122

# The panel's own block size; it answers 17 with 0, meaning this default.
DEFAULT_BLOCK = 202752
# The player's rate after power-on. Set again on exit, because the panel's own
# standby clip plays at the last rate set (at 60 it stutters).
POWER_ON_FPS = 30


def transparent_layer(size: tuple[int, int]) -> bytes:
    """A fully transparent PNG layer. Opaque black would hide the video completely."""
    return encode_png_rgba(Image.new("RGBA", size, (0, 0, 0, 0)), compress_level=9)


class DecoderHung(DeviceError):
    """The panel's decoder stopped taking pictures; reconnecting does not help."""


class PanelRestarting(DeviceError):
    """The panel restarts to clear a hung decoder; it is opened again once it is back."""

    retry_s = 1.0  # the main loop looks again soon, not with its growing pauses


class _Restart:
    """A restart of the panel under way, and how many came in a row.

    Kept per process, not per display object: the drivers make a new one each
    time the main loop opens the panel again.
    """

    IN_A_ROW = 3  # then: replug
    RESET_S = 300.0  # this long without a hang and the count starts again
    LEAVE_S, RETURN_S, SETTLE_S = 20.0, 90.0, 2.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.count = 0
        self.last = -1e9
        self.pid: int | None = None  # set while a restart is under way
        self.sent = self.gone = self.back = None

    def allowed(self) -> bool:
        if self.clock() - self.last > self.RESET_S:
            self.count = 0
        return self.count < self.IN_A_ROW

    def begin(self, pid: int) -> None:
        self.count += 1
        self.last = self.sent = self.clock()
        self.pid, self.gone, self.back = pid, None, None

    def wait(self, present: Callable[[int], bool]) -> None:
        """Raise :class:`PanelRestarting` until the panel has left the bus, is back
        and has settled; a :class:`DeviceError` when it does not come back."""
        if self.pid is None:
            return
        now, here = self.clock(), present(self.pid)
        if self.gone is None:
            if here and now - self.sent < self.LEAVE_S:
                raise PanelRestarting(t("the panel restarts to clear its hung video decoder"))
            self.gone = now  # left the bus (or never seemed to: go on after 20 s)
        if self.back is None:
            if not here:
                if now - self.gone < self.RETURN_S:
                    raise PanelRestarting(t("the panel restarts to clear its hung video decoder"))
                self.pid = None
                raise DeviceError(
                    t("the panel did not come back after its restart; replug it, please")
                )
            self.back = now
        if now - self.back < self.SETTLE_S:
            raise PanelRestarting(t("the panel restarts to clear its hung video decoder"))
        self.pid = None  # back and settled: open it as usual


RESTART = _Restart()


class VideoSession:
    """Start, picture blocks with flow control, and stop, on one open transport."""

    HIGH, LOW = 2, 1  # queue depth that starts waiting, and the depth to wait for
    POLL_EVERY = 2  # blocks between depth queries
    WAIT_S, GIVE_UP_S = 0.03, 1.5
    # Hang detection (SPUR II): a healthy block takes about 1 ms. When the median
    # of the last 40 exceeds 0.2 s, four depth readings 150 ms apart decide.
    HANG_WINDOW, HANG_MEDIAN_S = 40, 0.2
    HANG_READINGS, HANG_READING_GAP_S, HANG_DEPTH, HANG_SPREAD = 4, 0.15, 20, 2

    def __init__(
        self,
        transport: UsbTransport,
        native_size: tuple[int, int],
        device_fps: int,
        brightness: int,
        local_clip: str,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
        counters: Counters | None = None,
    ) -> None:
        self.transport = transport
        self.native_size = native_size
        self.device_fps = device_fps
        self.brightness = brightness
        self.local_clip = local_clip
        self.sleep, self.clock = sleep, clock
        self.block_size = DEFAULT_BLOCK
        self.blocks = self.bytes = self.waits = self.max_depth = 0  # of this session
        self.counters = counters or Counters()  # of the whole run, shared with the display
        self.send_times: deque[float] = deque(maxlen=self.HANG_WINDOW)

    def start(self) -> None:
        name = self.local_clip.encode("utf-8")
        if not name:
            # An empty name would erase the panel's standby clip setting.
            raise DeviceError(t("set --clip to the existing standby clip name"))
        command = self.transport.command
        command(CMD_PLAY_LOCAL, struct.pack(">I", len(name)) + bytes(4) + name)
        command(CMD_STOP_LOCAL)
        command(CMD_LOCAL_PLAYING)
        command(CMD_BRIGHTNESS, bytes([brightness_arg(self.brightness)]))
        layer = transparent_layer(self.native_size)
        command(CMD_UPLOAD_PNG, struct.pack(">I", len(layer)), layer, timeout_ms=5000)
        command(CMD_FRAME_RATE, bytes([self.device_fps]))
        reply = command(CMD_CHUNK_SIZE)
        size = int.from_bytes(reply[8:12], "big") if len(reply) >= 12 else 0
        if 0 < size <= 1024 * 1024:
            self.block_size = size

    def send(self, picture: bytes) -> None:
        """One picture; pictures larger than a block (rare keyframes) take several."""
        for start in range(0, len(picture), self.block_size):
            block = picture[start : start + self.block_size]
            began = self.clock()
            self.transport.command(
                CMD_H264_BLOCK, struct.pack(">I", len(block)), block, timeout_ms=5000
            )
            self.send_times.append(self.clock() - began)
            self.blocks += 1
            self.bytes += len(block)
            self.counters.blocks += 1
            self.counters.bytes += len(block)
            if self.blocks % self.POLL_EVERY == 0:
                depth = self.depth()
                if depth is not None and depth > self.HIGH:
                    self._wait_for_room()
            self.check_hang()

    def depth(self) -> int | None:
        reply = self.transport.query(CMD_QUEUE_DEPTH)
        if reply is None or len(reply) <= 8:
            return None
        self.max_depth = max(self.max_depth, reply[8])
        self.counters.max_depth = max(self.counters.max_depth, reply[8])
        return reply[8]

    def _wait_for_room(self) -> None:
        self.waits += 1
        self.counters.waits += 1
        started = self.clock()
        while self.clock() - started < self.GIVE_UP_S:
            self.sleep(self.WAIT_S)
            depth = self.depth()
            if depth is None or depth <= self.LOW:
                return
        log.debug("the panel's video queue did not go down within %.1f s", self.GIVE_UP_S)

    def check_hang(self) -> None:
        """Raise :class:`DecoderHung` when blocks go slowly *and* the queue stands high
        without moving. The median, not one slow block: waiting for room legitimately
        makes single sends slow, and a high queue that moves drains by itself."""
        times = self.send_times
        if len(times) < self.HANG_WINDOW or statistics.median(times) <= self.HANG_MEDIAN_S:
            return
        times.clear()  # decide once per window
        readings = []
        for i in range(self.HANG_READINGS):
            if i:
                self.sleep(self.HANG_READING_GAP_S)
            depth = self.depth()
            if depth is not None:
                readings.append(depth)
        log.warning("video blocks go slowly; queue depth %s", readings)
        if (
            len(readings) >= 2
            and max(readings) > self.HANG_DEPTH
            and max(readings) - min(readings) <= self.HANG_SPREAD
        ):
            raise DecoderHung(
                t(
                    "the panel's video decoder hangs (queue stuck at {depth}). "
                    "Unplug the panel and plug it in again.",
                    depth=max(readings),
                )
            )

    def stop(self, last_frame: bytes | None = None) -> None:
        """End the stream; ``last_frame`` (a native PNG) stays on the panel."""
        command = self.transport.command
        command(CMD_STOP_STREAM)
        command(CMD_FRAME_RATE, bytes([POWER_ON_FPS]))
        if last_frame:
            command(CMD_UPLOAD_PNG, struct.pack(">I", len(last_frame)), last_frame, timeout_ms=5000)


class TurzxVideoDisplay(TurzxUsbDisplay):
    """Every frame goes through ffmpeg into the panel's H.264 decoder.

    Three threads keep the render loop from ever waiting on USB: ffmpeg's
    writer and reader (see :class:`~libre_panel.devices.h264.Encoder`) and a
    sender for the 121 blocks. When the panel is slow, the small queue
    between reader and sender fills and ffmpeg's pipe slows the render loop
    down: fewer frames, never torn ones. A stopped ffmpeg is restarted; a USB
    error reconnects and starts the video again.
    """

    name = "turzx-video"
    streaming = True
    RESTARTS_PER_MINUTE = 3
    STALL_S = 2.0  # ffmpeg taking no frames this long, while the panel waits for pictures
    START_S = 15.0  # the same until its first picture: a cold start (virus scanner, busy PC)

    def __init__(self, config, model: PanelModel | None = None) -> None:
        super().__init__(config, model)
        self.video = config.video
        self.stream_fps = self.video.fps
        self.settings = EncoderSettings(
            fps=self.video.fps,
            crf=self.video.crf,
            preset=self.video.preset,
            keyframe_s=self.video.keyframe_s,
            maxrate=self.video.maxrate,
        )
        self.session: VideoSession | None = None
        self.encoder: Encoder | None = None
        self._ffmpeg: str | None = None
        self._pictures: queue.Queue[bytes] = queue.Queue(maxsize=2)
        self._sender: threading.Thread | None = None
        self._stop_sender = threading.Event()
        self._error: Exception | None = None
        self._generation = 0
        self._restarts: list[float] = []
        self._last_frame: Image.Image | None = None

    # -- connection ----------------------------------------------------------

    def open(self) -> None:
        self._ffmpeg = self._ffmpeg or find_ffmpeg(self.video.ffmpeg)  # before touching the panel
        restarting = RESTART.pid is not None
        RESTART.wait(UsbTransport.present)
        super().open()
        try:
            self._start_video()
        except Exception:
            self.close()
            raise
        self.restarted = restarting  # the main loop tells the services (panel-restarted)
        if restarting:
            log.info("the panel is back after its restart")

    def describe(self) -> str:
        return t("{panel}, video {fps} fps", panel=super().describe(), fps=self.stream_fps)

    def _start_video(self) -> None:
        native = (self.model.native_width, self.model.native_height) if self.model else (480, 1920)
        self.session = VideoSession(
            self.transport,
            native,
            self.video.device_fps,
            self.brightness if self.brightness is not None else self.config.brightness,
            self.video.local_clip,
            counters=self.counters,
        )
        self.session.start()
        self._error = None
        self._stop_sender = threading.Event()
        self._sender = threading.Thread(
            target=self._send_loop,
            args=(self.session, self._stop_sender),
            name="panel-video",
            daemon=True,
        )
        self._sender.start()

    def _stop_video(self) -> None:
        self._stop_encoder()
        self._stop_sender.set()
        if self._sender is not None:
            self._sender.join(10)
            self._sender = None
        self._discard_pictures()

    def _discard_pictures(self) -> None:
        """Throw away the pictures waiting for the sender: those of a stream that ended."""
        while True:
            try:
                self._pictures.get_nowait()
            except queue.Empty:
                return
            self.counters.drops += 1

    def _send_loop(self, session: VideoSession, stop: threading.Event) -> None:
        timing.raise_priority()  # a busy PC must not starve the panel (SPUR II)
        while not stop.is_set():
            try:
                picture = self._pictures.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                session.send(picture)
            except DeviceError as exc:
                self._error = exc
                if not isinstance(exc, DecoderHung):  # that is not the link
                    self.counters.usb_errors += 1
                return

    # -- encoder ---------------------------------------------------------------

    def _start_encoder(self, size: tuple[int, int]) -> None:
        self._stop_encoder()
        rotate = 270 if orientation_of(*size) == "landscape" else 180  # as to_native
        command = ffmpeg_command(self._ffmpeg, size, rotate, self.settings)
        self._generation += 1
        generation = self._generation
        raised = False

        def deliver(picture: bytes) -> None:
            nonlocal raised
            if not raised:  # ffmpeg's reader thread, forwarding the pictures: above normal too
                raised = True
                timing.raise_priority()
            while generation == self._generation and self._error is None:
                try:
                    self._pictures.put(picture, timeout=0.2)
                    return
                except queue.Full:
                    continue
            self.counters.drops += 1  # the stream was replaced, or the panel failed meanwhile

        self.encoder = Encoder(command, size, deliver)

    def _stop_encoder(self) -> None:
        if self.encoder is not None:
            self._generation += 1  # pictures of the old stream are dropped
            self.encoder.close()
            self.encoder = None
            self._discard_pictures()  # the new stream starts with a keyframe

    # -- frames ------------------------------------------------------------------

    def show(self, frame: Image.Image, region: tuple[int, int, int, int] | None = None) -> None:
        if self.transport is None or self.session is None:
            raise DeviceError("panel not open")
        if isinstance(self._error, DecoderHung):
            self._restart_panel(self._error)  # a new connection does not help
        if self._error is not None:
            self._heal(self._error)
        frame = frame.convert("RGB")
        self._last_frame = frame
        data = frame.tobytes()
        encoder = self.encoder
        if encoder is None or encoder.size != frame.size or not encoder.alive:
            if encoder is not None and encoder.size == frame.size:
                self._restarting(encoder.reason())
            self._start_encoder(frame.size)
        try:
            self._feed(data)
        except EncoderStopped as exc:
            if self._error is not None:
                return  # the panel failed meanwhile; the next frame reconnects
            self._restarting(str(exc))
            self._start_encoder(frame.size)
            self._feed(data)

    def _feed(self, data: bytes) -> None:
        """Hand a frame to ffmpeg. Waiting is right while the panel sets the pace
        (the pictures queue is full; USB timeouts bound that); ffmpeg taking nothing
        while the panel waits for pictures has stalled. Before its first picture
        ffmpeg may still be starting, which takes longer: restarting it then would
        only drop the frames it already has."""
        idle = 0.0
        while True:
            try:
                self.encoder.write(data, timeout=0.25)
                return
            except EncoderStopped:
                if not self.encoder.alive or self._error is not None:
                    raise
                if self._pictures.full():
                    idle = 0.0
                    continue
                idle += 0.25
                limit = self.STALL_S if self.encoder.pictures else self.START_S
                if idle >= limit:
                    raise EncoderStopped(f"ffmpeg took no frames for {limit:g} s") from None

    def _restarting(self, reason: str) -> None:
        """Count an ffmpeg that stopped by itself (not one closed for a reconnect)."""
        now = time.monotonic()
        self.counters.ffmpeg_restarts += 1
        self._restarts = [t0 for t0 in self._restarts if now - t0 < 60] + [now]
        if len(self._restarts) > self.RESTARTS_PER_MINUTE:
            raise DeviceError(t("ffmpeg keeps stopping: {reason}", reason=reason))
        log.warning("ffmpeg stopped (%s); starting it again", reason)

    def _restart_panel(self, hung: DecoderHung) -> None:
        """Restart the panel (11) to clear its hung decoder, as SPUR II does; the
        main loop opens it again once it is back. Raises :class:`PanelRestarting`,
        or ``hung`` itself when restarts did not help (then: replug)."""
        if not RESTART.allowed():
            raise hung
        RESTART.begin(self.transport.pid)
        self.counters.panel_restarts += 1
        log.warning(
            "the panel's video decoder hangs; restarting the panel (%d of %d in a row)",
            RESTART.count,
            RESTART.IN_A_ROW,
        )
        self._stop_video()
        try:
            self.transport.query(CMD_RESTART, timeout_ms=200)  # it may be gone before it answers
        except DeviceError:
            pass
        self.session, self._error = None, None
        super().close()
        raise PanelRestarting(t("the panel restarts to clear its hung video decoder"))

    def _heal(self, exc: Exception) -> None:
        """Reconnect after a USB error and start the video again (a full start)."""
        log.warning("panel stopped answering (%s); reconnecting", exc)
        self._stop_video()
        super().close()
        self.session = None
        super().open()
        self._start_video()

    def close(self) -> None:
        self._stop_video()
        if self.transport is not None and self.session is not None and self._error is None:
            try:
                last = encode_frame(to_native(self._last_frame)) if self._last_frame else None
            except FrameError:
                last = None  # too detailed for a PNG: the video's last picture stays
            try:
                self.session.stop(last)
            except DeviceError as exc:
                log.debug("stopping the video: %s", exc)
        if self.session is not None and self.session.blocks:
            log.info(
                "video: %d blocks, %.1f MB, waited %d times, deepest queue %d",
                self.session.blocks,
                self.session.bytes / 1e6,
                self.session.waits,
                self.session.max_depth,
            )
        self.session = None
        super().close()
