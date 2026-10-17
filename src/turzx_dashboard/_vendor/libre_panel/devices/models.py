# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from Libre Panel: imports relocated; see THIRD_PARTY.md.
"""Catalog of known panels.

Sizes, native resolutions and USB identifiers are hardware facts collected
from public sources (vendor listings and the turing-smart-screen-python wiki);
SPUR II testing adds to this. ``driver`` says whether Libre Panel can drive the
panel yet. Native resolution is given in portrait, as the panels report it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

TURING = "Turing Smart Screen / TURZX"

EDGES = ("top", "right", "bottom", "left")

# Protocol families. Panels in one family share a driver. All plug in with a USB
# cable; "USB serial" panels show up as a virtual COM port (USB CDC), the
# others as a USB device of their own.
PROTOCOLS = {
    "serial-a": "USB serial (CH552T), Turing rev. A",
    "serial-b": "USB serial (CH552T), XuanFang rev. B",
    "serial-c": "USB serial, Turing rev. C",
    "serial-d": "USB serial, Kipye rev. D",
    "serial-weact": "USB serial, WeAct Studio",
    "usb-turing": "USB bulk (VID 0x1CBE), Turing V1.x hardware",
}


@dataclass(frozen=True)
class PanelModel:
    id: str
    vendor: str
    name: str
    size_inch: float
    native_width: int
    native_height: int
    protocol: str
    shape: str = "rect"  # "rect" or "round"
    usb_ids: tuple[tuple[int, int], ...] = ()
    serial_numbers: tuple[str, ...] = ()
    # "supported": `libre-panel doctor` passed on this model, or its owners report it runs.
    # "unverified": driver exists, protocol proven on hardware (SPUR II, or
    # turing-smart-screen-python for serial panels), this model not confirmed yet.
    # "planned": no driver yet.
    driver: str = "planned"
    notes: str = ""
    # Pixels of the framebuffer (native, portrait) behind the bezel:
    # top, right, bottom, left. Measured with the ruler card of `libre-panel doctor`.
    hidden: tuple[int, int, int, int] = (0, 0, 0, 0)
    # Video mode (H.264) proven on this model: ``video.mode = "auto"`` uses it here.
    video: bool = False

    def size(self, orientation: str) -> tuple[int, int]:
        """Frame size for ``portrait`` or ``landscape``."""
        short, long = sorted((self.native_width, self.native_height))
        return (long, short) if orientation == "landscape" else (short, long)

    def hidden_edges(self, orientation: str) -> dict[str, int]:
        """The hidden strips at the edges of a frame in ``orientation``.

        Data for layouts and the editor's guide; nothing crops or moves a frame.
        Frames reach the framebuffer turned as the USB driver turns them: a
        landscape frame a quarter turn clockwise (its top lands on the
        framebuffer's right edge), a portrait frame half a turn.
        """
        top, right, bottom, left = self.hidden
        if orientation == "landscape":
            return {"top": right, "right": bottom, "bottom": left, "left": top}
        return {"top": bottom, "right": left, "bottom": top, "left": right}

    @property
    def label(self) -> str:
        shape = " round" if self.shape == "round" else ""
        return f'{self.name} {self.size_inch:g}"{shape}'

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["label"] = self.label
        data["usb_ids"] = [f"{v:04x}:{p:04x}" for v, p in self.usb_ids]
        data["landscape"] = list(self.size("landscape"))
        data["portrait"] = list(self.size("portrait"))
        data["hidden"] = {o: self.hidden_edges(o) for o in ("landscape", "portrait")}
        data["protocol_name"] = PROTOCOLS.get(self.protocol, self.protocol)
        return data


_REV_C_IDS = ((0x1A86, 0xCA21), (0x0525, 0xA4A7), (0x1D6B, 0x0121), (0x1D6B, 0x0106))

MODELS: tuple[PanelModel, ...] = (
    PanelModel(
        "turing-2.1",
        TURING,
        "Turing",
        2.1,
        480,
        480,
        "serial-c",
        shape="round",
        usb_ids=_REV_C_IDS,
        serial_numbers=("CT21INCH", "20080411"),
    ),
    PanelModel(
        "turing-2.8-usb",
        TURING,
        "Turing",
        2.8,
        480,
        480,
        "usb-turing",
        shape="round",
        usb_ids=((0x1CBE, 0x0028),),
        notes="V1.x hardware",
    ),
    PanelModel(
        "turing-3.5",
        TURING,
        "Turing",
        3.5,
        320,
        480,
        "serial-a",
        usb_ids=((0x1A86, 0x5722),),
        serial_numbers=("USB35INCHIPS", "USB35INCHIPSV2"),
    ),
    PanelModel(
        "turing-4.6-usb",
        TURING,
        "Turing",
        4.6,
        320,
        960,
        "usb-turing",
        usb_ids=((0x1CBE, 0x0046),),
    ),
    PanelModel(
        "turing-5",
        TURING,
        "Turing",
        5,
        480,
        800,
        "serial-c",
        usb_ids=_REV_C_IDS,
        # Some are sold as "UsbPCMonitor 5"" and sleep as CT21INCH, like the 2.1".
        serial_numbers=("USB7INCH", "CT21INCH", "20080411"),
        notes='also sold as UsbPCMonitor 5"',
    ),
    PanelModel(
        "turing-5.2-usb",
        TURING,
        "Turing",
        5.2,
        720,
        1280,
        "usb-turing",
        usb_ids=((0x1CBE, 0x0050),),
    ),
    PanelModel(
        "turing-8-usb",
        TURING,
        "Turing",
        8,
        800,
        1280,
        "usb-turing",
        usb_ids=((0x1CBE, 0x0080),),
    ),
    PanelModel(
        "turing-8.8",
        TURING,
        "Turing",
        8.8,
        480,
        1920,
        "serial-c",
        usb_ids=_REV_C_IDS,
        serial_numbers=("CT88INCH", "20080411"),
        notes="V0.x hardware",
    ),
    PanelModel(
        "turing-8.8-usb",
        TURING,
        "Turing",
        8.8,
        480,
        1920,
        "usb-turing",
        usb_ids=((0x1CBE, 0x0088),),
        notes="V1.x hardware; runs, as its owners report",
    ),
    PanelModel(
        # The framebuffer is 480x1920, but the bezel hides its last 18 columns:
        # 1920x462 are visible, the reference library's size (measured with a
        # 1 px ruler on the 9.2"; in landscape the strip is at the top).
        "turing-9.2-usb",
        TURING,
        "Turing",
        9.2,
        480,
        1920,
        "usb-turing",
        usb_ids=((0x1CBE, 0x0092),),
        notes="protocol verified on hardware",
        hidden=(0, 18, 0, 0),
        video=True,
    ),
    PanelModel(
        "turing-12.3-usb",
        TURING,
        "Turing",
        12.3,
        720,
        1920,
        "usb-turing",
        usb_ids=((0x1CBE, 0x0123),),
    ),
    PanelModel(
        "usbpcmonitor-3.5",
        "UsbPCMonitor",
        "UsbPCMonitor",
        3.5,
        320,
        480,
        "serial-a",
        usb_ids=((0x1A86, 0x5722),),
    ),
    PanelModel(
        "usbpcmonitor-5",
        "UsbPCMonitor",
        "UsbPCMonitor",
        5,
        480,
        800,
        "serial-a",
        usb_ids=((0x1A86, 0x5722),),
        notes='USB id 1a86:5722; one with serial number CT21INCH is "turing-5"',
    ),
    PanelModel(
        "usbpcmonitor-7",
        "UsbPCMonitor",
        "UsbPCMonitor",
        7,
        600,
        1024,
        "serial-a",
        usb_ids=((0x1A86, 0x5722),),
    ),
    PanelModel(
        "xuanfang-3.5",
        "XuanFang",
        "XuanFang rev. B / flagship",
        3.5,
        320,
        480,
        "serial-b",
        usb_ids=((0x1A86, 0x5722),),
        serial_numbers=("2017-2-25",),
    ),
    PanelModel(
        "kipye-3.5",
        "Kipye",
        "Kipye Qiye",
        3.5,
        320,
        480,
        "serial-d",
        usb_ids=((0x454D, 0x4E41),),
    ),
    PanelModel(
        "weact-3.5",
        "WeAct Studio",
        "WeAct Display FS V1",
        3.5,
        320,
        480,
        "serial-weact",
        usb_ids=((0x1A86, 0xFE0C),),
        notes="serial number starts with AB",
    ),
    PanelModel(
        "weact-0.96",
        "WeAct Studio",
        "WeAct Display FS V1",
        0.96,
        80,
        160,
        "serial-weact",
        usb_ids=((0x1A86, 0xFE0C),),
        notes="serial number starts with AD",
    ),
)

# Families with a driver (devices/turzx.py lists them); the rest is planned.
_DRIVER_STATUS = {
    protocol: "unverified"
    for protocol in ("usb-turing", "serial-a", "serial-b", "serial-c", "serial-d", "serial-weact")
}
# Models confirmed on the panel: with `libre-panel doctor`, or by an owner who reports it runs.
_CONFIRMED: set[str] = {
    "turing-9.2-usb",  # stills and 50 fps video, for hours on end
    "turing-8.8-usb",  # "works wonderfully", an owner's report in the TURZX forum
}
MODELS = tuple(
    replace(
        m, driver="supported" if m.id in _CONFIRMED else _DRIVER_STATUS.get(m.protocol, m.driver)
    )
    for m in MODELS
)

_BY_ID = {m.id: m for m in MODELS}


def get_model(model_id: str) -> PanelModel:
    try:
        return _BY_ID[model_id]
    except KeyError:
        raise KeyError(f"unknown panel model {model_id!r}") from None


def find_model(model_id: str | None) -> PanelModel | None:
    return _BY_ID.get(model_id or "")


def models_for_usb(vid: int | None, pid: int | None, serial: str | None = None) -> list[PanelModel]:
    """Candidate models for a connected device.

    Several serial models share a USB bridge chip, so the result can hold more
    than one entry; a serial-number match narrows it down, and the driver's
    handshake settles the rest.
    """
    if vid is None or pid is None:
        return []
    matches = [m for m in MODELS if (vid, pid) in m.usb_ids]
    if serial:
        exact = [m for m in matches if serial in m.serial_numbers]
        if exact:
            return exact
    return matches


def orientation_of(width: int, height: int) -> str:
    return "landscape" if width > height else "portrait"


def model_for_size(width: int, height: int) -> PanelModel | None:
    """First Turing model whose native resolution matches, in either orientation."""
    wanted = tuple(sorted((width, height)))
    for model in MODELS:
        if tuple(sorted((model.native_width, model.native_height))) == wanted:
            return model
    return None
