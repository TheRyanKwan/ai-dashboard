# SPDX-License-Identifier: GPL-3.0-or-later
# From Libre Panel tests; imports relocated. See THIRD_PARTY.md.
"""A simulated Turing V1.x USB panel for tests.

It checks every packet the way the real device needs it (DES, magic, trailer,
size field, RGBA PNG) and answers with echo + ACK, including the 512-byte
replies whose zero-length packet trips up readers that ask for exactly 512.

The video path is simulated too: 110 must carry a name, 121 blocks go into a
ring of five (overflow silently drops the oldest, as on the panel), and a
player takes one block per frame at the rate set with 15. 122 answers with
the queue depth, optionally late.
"""

from __future__ import annotations

import io
import struct
import time
from collections.abc import Callable, Iterator

from PIL import Image

from turzx_dashboard._vendor.libre_panel.devices.turzx_usb import ACK, READ_LEN, UsbTransport, decode_packet


class Unplugged(Exception):
    """Stands in for usb.core.USBError."""


class FakePanel:
    RING = 5
    MAX_BLOCK = 202752

    def __init__(
        self, pid: int = 0x0092, native=(480, 1920), clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.pid = pid
        self.native = native
        self.clock = clock
        self.commands: list[int] = []
        self.frames: list[Image.Image] = []
        self.brightness: list[int] = []
        self.pending: list[bytes] = []
        self.unplugged = False
        self.fail_next_writes = 0
        # video
        self.fps = 30  # the player's rate after power-on
        self.fps_log: list[int] = []
        self.clip_names: list[str] = []
        self.video_ready = False  # 110 cleared the framebuffer; without it the screen stays black
        self.blocks: list[bytes] = []  # every 121 payload, in order
        self.queue: list[tuple[float, bytes]] = []
        self.played = 0
        self.overwritten = 0
        self.busy_until = 0.0
        self.late_status = 0  # the next n 122 answers come late (after the next command)
        self._late: list[bytes] = []
        self.hung_depths: Iterator[int] | None = None  # a hung decoder: fixed answers to 122
        self.block_delay = 0.0  # a slow panel: seconds each 121 block takes
        # 11 restarts the panel: off the bus for ``restart_away_s``, then back as after power-on
        self.restart_away_s = 0.3
        self.restarts = 0
        self.away_until = 0.0
        self.recovers = True  # False: the restart does not clear the hung decoder

    @property
    def present(self) -> bool:
        """On the bus (not unplugged, not in the middle of a restart)."""
        return not self.unplugged and time.monotonic() >= self.away_until

    def _restart(self) -> None:
        self.restarts += 1
        self.away_until = time.monotonic() + self.restart_away_s
        self.fps, self.video_ready, self.queue, self.pending = 30, False, [], []
        if self.recovers:
            self.hung_depths = None

    def _play(self) -> None:
        """The panel's player: one block per frame, taken when the previous frame is done."""
        if self.hung_depths is not None:
            return
        now, frame = self.clock(), 1 / self.fps
        while self.queue:
            start = max(self.busy_until, self.queue[0][0])
            if start > now:
                break
            self.queue.pop(0)
            self.played += 1
            self.busy_until = start + frame

    def stream(self) -> bytes:
        """Everything received on the video path."""
        return b"".join(self.blocks)

    # endpoint OUT
    def write(self, data: bytes, timeout: int) -> None:
        import usb.core

        if not self.present or self.fail_next_writes:
            self.fail_next_writes = max(0, self.fail_next_writes - 1)
            raise usb.core.USBError("No such device (it may have been disconnected)", errno=19)
        data = bytes(data)
        plain = decode_packet(data[:512])  # checks trailer and magic
        cmd = plain[0]
        self.commands.append(cmd)
        payload = data[512:]
        self.pending.extend(self._late)  # a late answer arrives before the next one
        self._late.clear()
        if cmd in (101, 102, 121):
            size = struct.unpack(">I", plain[8:12])[0]
            assert size == len(payload), f"size field {size} != payload {len(payload)}"
        if cmd == 102:
            assert payload[25] == 6, "panel needs RGBA PNGs (colour type 6)"
            with Image.open(io.BytesIO(payload)) as png:
                assert png.size == self.native, f"frame {png.size} != framebuffer {self.native}"
                self.frames.append(png.copy())
        if cmd == 14:
            assert 0 <= plain[8] <= 102
            self.brightness.append(plain[8])
        reply = bytearray(bytes([cmd, ACK]) + (b"turzx_00" if cmd == 10 else b"")).ljust(512, b"\0")
        if cmd == 110:
            size = struct.unpack(">I", plain[8:12])[0]
            name = bytes(plain[16 : 16 + size])
            assert name, "an empty name erases the panel's standby clip setting"
            self.clip_names.append(name.decode("utf-8"))
            self.video_ready = True
        if cmd == 15:
            assert 1 <= plain[8] <= 120
            self.fps = plain[8]
            self.fps_log.append(plain[8])
        if cmd == 121:
            assert len(payload) <= self.MAX_BLOCK, "block larger than the panel's buffer"
            assert self.video_ready, "121 before 110: the screen stays black"
            if self.block_delay:
                time.sleep(self.block_delay)
            self._play()
            if len(self.queue) >= self.RING:  # the ring overwrites an unread block
                self.queue.pop(0)
                self.overwritten += 1
            self.queue.append((self.clock(), payload))
            self.blocks.append(payload)
        if cmd == 122:
            self._play()
            depth = next(self.hung_depths) if self.hung_depths else len(self.queue)
            reply[8] = depth
            if self.late_status:
                self.late_status -= 1
                self._late.append(bytes(reply))
                return
        if cmd == 123:
            self.queue.clear()
            self.video_ready = False
        if cmd == 11:
            self._restart()  # gone before it answers
            return
        self.pending.append(bytes(reply))

    # endpoint IN
    def read(self, length: int, timeout: int) -> bytes:
        import usb.core

        assert length >= READ_LEN, "read at least 1024 bytes or the ZLP stays in the pipe"
        if not self.present or not self.pending:
            raise usb.core.USBTimeoutError("Operation timed out")
        return self.pending.pop(0)


def transport_for(panel: FakePanel) -> UsbTransport:
    return UsbTransport(device=None, ep_out=panel, ep_in=panel, pid=panel.pid)
