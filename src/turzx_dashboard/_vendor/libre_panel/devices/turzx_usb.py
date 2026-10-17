# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from Libre Panel: imports relocated; see THIRD_PARTY.md.
"""Turing / TURZX V1.x USB panels (VID 0x1CBE): 2.8" round, 4.6", 5.2", 8", 8.8", 9.2", 12.3".

The protocol comes from the GPL-3.0 reference implementation
turing-smart-screen-python (Matthieu Houdebine) and was verified on real
hardware (9.2", PID 0x0092) by the SPUR II project, which also found the
fixes marked below. See docs/protocol/turzx-usb.md. ``libre-panel doctor``
checks a panel end to end.

Wire format: every command is one 512-byte packet. 500 bytes of plain text
([0] command id, [2:4] magic 1A 6D, [4:8] milliseconds since local midnight,
little endian, [8:] arguments) are DES-CBC encrypted with key = IV =
``slv3tuzx`` (zero padded to 504 bytes); bytes 510/511 carry the trailer
A1 1A. A payload (PNG, H.264) follows the packet in the same bulk transfer.
The panel answers on the bulk IN endpoint: byte 0 echoes the command id,
0xC8 means "accepted" (not "displayed").

The live path uses only harmless commands. Restart (11) is sent by the video
mode's hang detection alone; the panel's own files and settings (its boot logo
and standby clip: 40, 42, 98, 99, 113, 125) are written by
devices/panel_files.py, only when the person asks for it. Firmware is never
touched, and 12 (halt) and 13 are never sent.
"""

from __future__ import annotations

import io
import logging
import struct
import sys
import threading
import time
from datetime import datetime
from typing import Any

from PIL import Image

from turzx_dashboard._vendor.libre_panel.devices.base import DeviceError, Display, FrameError
from turzx_dashboard._vendor.libre_panel.devices.models import MODELS, PanelModel, orientation_of
from turzx_dashboard._vendor.libre_panel.i18n import t

log = logging.getLogger(__name__)

VENDOR_ID = 0x1CBE
DES_KEY = b"slv3tuzx"
MAGIC = b"\x1a\x6d"
TRAILER = b"\xa1\x1a"
ACK = 0xC8
# Replies are exactly 512 bytes = wMaxPacketSize, so USB ends them with a
# zero-length packet. Reading 512 leaves that ZLP in the pipe and every later
# reply is off by one until the device stalls (found by SPUR II). Asking for
# more than one packet consumes it.
READ_LEN = 1024

# Larger payloads make the panel time out (reference library).
MAX_PAYLOAD = 1024 * 1024

CMD_SYNC = 10
CMD_BRIGHTNESS = 14
CMD_FRAME_RATE = 15
CMD_UPLOAD_PNG = 102
CMD_STOP_STREAM = 123
# Restarts the panel (it is back after about 5 s). Sent only to clear a hung
# video decoder, the one fault nothing else fixes but replugging (turzx_video).
CMD_RESTART = 11

USB_PIDS = {pid: m for m in MODELS if m.protocol == "usb-turing" for _vid, pid in m.usb_ids}


def present_model() -> PanelModel | None:
    """The model of a USB panel on the bus, without opening it (None: none there, or no pyusb)."""
    for pid, model in USB_PIDS.items():
        if UsbTransport.present(pid):
            return model
    return None


def ms_since_midnight(now: float | None = None) -> int:
    # From the local wall clock, not via mktime: Windows' mktime fails for
    # midnights before 1970, i.e. for "now" near the epoch east of UTC.
    local = datetime.fromtimestamp(time.time() if now is None else now)
    seconds = (local.hour * 60 + local.minute) * 60 + local.second
    return (seconds * 1000 + local.microsecond // 1000) & 0xFFFFFFFF


def _cipher() -> Any:
    try:
        from Crypto.Cipher import DES
    except ImportError as exc:
        raise DeviceError("pycryptodome is missing: pip install 'turzx-dashboard[usb]'") from exc
    return DES.new(DES_KEY, DES.MODE_CBC, iv=DES_KEY)


def build_packet(cmd: int, args: bytes = b"", now: float | None = None) -> bytes:
    if not 0 <= cmd <= 255:
        raise ValueError("command id must fit in one byte")
    if len(args) > 492:
        raise ValueError("arguments too long")
    plain = bytearray(500)
    plain[0] = cmd
    plain[2:4] = MAGIC
    plain[4:8] = struct.pack("<I", ms_since_midnight(now))
    plain[8 : 8 + len(args)] = args
    encrypted = _cipher().encrypt(bytes(plain).ljust(504, b"\x00"))
    packet = bytearray(512)
    packet[: len(encrypted)] = encrypted
    packet[510:512] = TRAILER
    return bytes(packet)


def decode_packet(packet: bytes) -> bytes:
    """Inverse of build_packet (tests, capture analysis)."""
    if len(packet) < 512 or packet[510:512] != TRAILER:
        raise ValueError("not a command packet")
    try:
        from Crypto.Cipher import DES
    except ImportError as exc:
        raise DeviceError("pycryptodome is missing") from exc
    plain = DES.new(DES_KEY, DES.MODE_CBC, iv=DES_KEY).decrypt(bytes(packet[:504]))
    if plain[2:4] != MAGIC:
        raise ValueError("bad magic")
    return plain[:500]


def brightness_arg(percent: int) -> int:
    """0-100 % -> the panel's 0-102 scale."""
    return int(max(0, min(100, percent)) / 100 * 102)


def encode_png_rgba(image: Image.Image, compress_level: int = 2) -> bytes:
    # The panel only decodes PNG colour type 6 (RGBA). RGB frames come out
    # shifted by a byte per pixel and tiled four times (SPUR II).
    buffer = io.BytesIO()
    image.convert("RGBA").save(buffer, format="PNG", compress_level=compress_level)
    return buffer.getvalue()


def encode_frame(image: Image.Image) -> bytes:
    """PNG for one frame, within the panel's payload limit.

    Level 2 is fast (about 12 ms for 1920x480, SPUR II); busy frames fall
    back to level 9. JPEG is no fallback: the 9.2" panel shows it wrongly.
    """
    png = encode_png_rgba(image, 2)
    if len(png) > MAX_PAYLOAD:
        png = encode_png_rgba(image, 9)
    if len(png) > MAX_PAYLOAD:
        raise FrameError(
            f"frame too detailed for the panel ({len(png) // 1024} KB, limit "
            f"{MAX_PAYLOAD // 1024} KB); use fewer photos or gradients in the theme"
        )
    return png


def to_native(frame: Image.Image) -> Image.Image:
    """Rotate a theme frame into the panel's portrait framebuffer."""
    if orientation_of(*frame.size) == "landscape":
        return frame.transpose(Image.Transpose.ROTATE_270)  # verified on 9.2"
    return frame.transpose(Image.Transpose.ROTATE_180)  # per reference library, unverified


def access_hint(exc: Exception) -> str | None:
    """Explain the usual reasons a panel cannot be opened, per operating system."""
    errno = getattr(exc, "errno", None)
    text = str(exc).lower()
    busy = errno == 16 or "busy" in text
    denied = errno == 13 or "access" in text or "permission" in text
    if sys.platform == "win32" and (busy or denied):
        return t(
            "the panel is in use by another program, usually the TURZX app. "
            "Quit it (tray icon -> Exit) and try again."
        )
    if busy:
        return t("the panel is in use by another program (TURZX or other monitor software).")
    if denied and sys.platform.startswith("linux"):
        return t(
            "no permission to use the panel. Install the Linux udev rule from "
            "turzx-dashboard's README, reload udev rules, then replug the panel."
        )
    if denied:
        return t("no permission to use the panel.")
    return None


def _backend() -> Any:
    try:
        import libusb_package  # bundles libusb-1.0 for Windows

        return libusb_package.get_libusb1_backend()
    except ImportError:
        return None


class UsbTransport:
    """Bulk transport on interface 0 (EP 0x01 OUT, EP 0x81 IN, 512-byte packets).

    Thread-safe: each command and its reply form one unit.
    """

    def __init__(self, device: Any, ep_out: Any, ep_in: Any, pid: int) -> None:
        self.device, self.ep_out, self.ep_in, self.pid = device, ep_out, ep_in, pid
        self._lock = threading.RLock()
        self._late = 0  # replies to queries we stopped waiting for; skipped when they come

    @staticmethod
    def present(pid: int) -> bool:
        """Whether the panel with this product id is on the bus (without opening it)."""
        try:
            import usb.core
        except ImportError:
            return False
        try:
            return usb.core.find(idVendor=VENDOR_ID, idProduct=pid, backend=_backend()) is not None
        except usb.core.NoBackendError:
            return False

    @classmethod
    def open(cls, pid: int | None = None) -> UsbTransport:
        try:
            import usb.core
            import usb.util
        except ImportError as exc:
            raise DeviceError("pyusb is missing: pip install 'turzx-dashboard[usb]'") from exc
        backend = _backend()
        pids = [pid] if pid is not None else list(USB_PIDS)
        device = None
        try:
            for candidate in pids:
                device = usb.core.find(idVendor=VENDOR_ID, idProduct=candidate, backend=backend)
                if device is not None:
                    pid = candidate
                    break
        except usb.core.NoBackendError as exc:
            raise DeviceError("libusb not found: pip install libusb-package") from exc
        if device is None or pid is None:
            raise DeviceError(
                t(
                    "no Turing/TURZX USB panel found. Is it plugged in, and is the TURZX app "
                    "closed? (it holds the device exclusively)"
                )
            )
        try:
            if sys.platform.startswith("linux") and device.is_kernel_driver_active(0):
                device.detach_kernel_driver(0)
            device.set_configuration()
        except usb.core.USBError as exc:
            hint = access_hint(exc)
            if hint:
                raise DeviceError(hint) from exc
            log.debug("set_configuration: %s", exc)  # already configured is fine
        try:
            interface = usb.util.find_descriptor(
                device.get_active_configuration(), bInterfaceNumber=0
            )
            if interface is None:
                raise DeviceError("USB interface 0 not found")
            usb.util.claim_interface(device, 0)
        except usb.core.USBError as exc:
            raise DeviceError(access_hint(exc) or f"cannot open the panel: {exc}") from exc

        def endpoint(direction: int) -> Any:
            return usb.util.find_descriptor(
                interface,
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == direction,
            )

        ep_out, ep_in = endpoint(usb.util.ENDPOINT_OUT), endpoint(usb.util.ENDPOINT_IN)
        if ep_out is None or ep_in is None:
            raise DeviceError("bulk endpoints not found")
        return cls(device, ep_out, ep_in, pid)

    def drain(self, timeout_ms: int = 50, limit: int = 64) -> int:
        """Discard replies left over from an earlier (crashed) session."""
        import usb.core

        dropped = 0
        with self._lock:
            for _ in range(limit):
                try:
                    self.ep_in.read(READ_LEN, timeout_ms)
                    dropped += 1
                except usb.core.USBError:
                    break
            self._late = 0
        return dropped

    def _reply(self, cmd: int, timeout_ms: int) -> bytes:
        reply = bytes(self.ep_in.read(READ_LEN, timeout_ms))
        while self._late and reply[:1] != bytes([cmd]):
            self._late -= 1  # the late answer to an earlier query
            reply = bytes(self.ep_in.read(READ_LEN, timeout_ms))
        return reply

    def command(
        self, cmd: int, args: bytes = b"", payload: bytes = b"", timeout_ms: int = 2000
    ) -> bytes:
        import usb.core

        with self._lock:
            try:
                self.ep_out.write(build_packet(cmd, args) + payload, timeout_ms)
                reply = self._reply(cmd, timeout_ms)
            except usb.core.USBError as exc:
                raise DeviceError(f"USB error on command {cmd}: {exc}") from exc
        if not reply or reply[0] != cmd:
            raise DeviceError(f"command {cmd}: unexpected reply {reply[:16].hex(' ')}")
        if ACK not in (reply[1:2] + reply[8:9]):
            raise DeviceError(f"command {cmd} not accepted: {reply[:16].hex(' ')}")
        return reply

    def ask(self, cmd: int, args: bytes = b"", timeout_ms: int = 2000, echo: bool = True) -> bytes:
        """A command whose reply carries data rather than the accepted mark (a file's size,
        a piece of a folder's listing): the reply as it comes, for the caller to read. With
        ``echo`` it must begin with the command's number; without, it is taken as it is."""
        import usb.core

        with self._lock:
            try:
                self.ep_out.write(build_packet(cmd, args), timeout_ms)
                reply = self._reply(cmd, timeout_ms)
            except usb.core.USBError as exc:
                raise DeviceError(f"USB error on command {cmd}: {exc}") from exc
        if echo and reply[:1] != bytes([cmd]):
            raise DeviceError(f"command {cmd}: unexpected reply {reply[:16].hex(' ')}")
        return reply

    def query(self, cmd: int, timeout_ms: int = 400) -> bytes | None:
        """A status request. While the panel decodes video it sometimes answers
        late; then this returns None and the late answer is skipped later."""
        import usb.core

        with self._lock:
            try:
                self.ep_out.write(build_packet(cmd), timeout_ms)
            except usb.core.USBError as exc:
                raise DeviceError(f"USB error on command {cmd}: {exc}") from exc
            try:
                reply = self._reply(cmd, timeout_ms)
            except usb.core.USBTimeoutError:
                self._late = min(self._late + 1, 8)
                return None
            except usb.core.USBError as exc:
                raise DeviceError(f"USB error on command {cmd}: {exc}") from exc
        if reply[:1] != bytes([cmd]):
            raise DeviceError(f"command {cmd}: unexpected reply {reply[:16].hex(' ')}")
        return reply

    def sync(self, attempts: int = 3) -> bytes:
        last: Exception | None = None
        for _ in range(attempts):
            self.drain()
            try:
                return self.command(CMD_SYNC)
            except DeviceError as exc:
                last = exc
        raise DeviceError(f"panel does not answer the sync command: {last}")

    def close(self) -> None:
        try:
            import usb.util
        except ImportError:
            return
        for release in (
            lambda: usb.util.release_interface(self.device, 0),
            lambda: usb.util.dispose_resources(self.device),
        ):
            try:
                release()
            except Exception:  # the device may already be gone; closing must never raise
                pass


class TurzxUsbDisplay(Display):
    """Bitmap path: every frame is sent as a full RGBA PNG (about 9 fps on 9.2").

    The smooth 25-50 fps path (H.264 background layer, command 110 + 121) is
    :class:`~libre_panel.devices.turzx_video.TurzxVideoDisplay`.
    """

    name = "turzx-usb"

    def __init__(self, config, model: PanelModel | None = None) -> None:
        super().__init__(config)
        self.model = model
        self.transport: UsbTransport | None = None
        self.brightness: int | None = None

    def open(self) -> None:
        pid = self.model.usb_ids[0][1] if self.model and self.model.usb_ids else None
        self.transport = UsbTransport.open(pid)
        self.model = self.model or USB_PIDS.get(self.transport.pid)
        try:
            self.transport.sync()
        except DeviceError:
            self.close()
            raise
        # (The bytes after the handshake reply's first two change with every
        # connection: no identifier worth logging.)
        log.info(
            "connected to %s",
            self.model.label if self.model else f"PID {self.transport.pid:04x}",
        )

    def describe(self) -> str:
        return self.model.label if self.model else t("TURZX USB panel")

    def set_brightness(self, percent: int) -> None:
        self.brightness = percent
        if self.transport:
            self.transport.command(CMD_BRIGHTNESS, bytes([brightness_arg(percent)]))

    def show(self, frame: Image.Image, region: tuple[int, int, int, int] | None = None) -> None:
        png = encode_frame(to_native(frame))
        try:
            self._send_png(png)
        except DeviceError as exc:
            # A replug, standby or a crashed earlier session leaves the pipe out of
            # step; one fresh connection with a resync usually fixes it (SPUR II).
            log.warning("panel stopped answering (%s); reconnecting", exc)
            self.counters.usb_errors += 1
            self.close()
            self.open()
            if self.brightness is not None:
                self.set_brightness(self.brightness)
            self._send_png(png)

    def _send_png(self, png: bytes) -> None:
        if self.transport is None:
            raise DeviceError("panel not open")
        self.transport.command(CMD_UPLOAD_PNG, struct.pack(">I", len(png)), png, timeout_ms=5000)
        self.counters.bytes += len(png)

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
