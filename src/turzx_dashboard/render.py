# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose the original layout into complete, independently animated frames."""

import math
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

from ._vendor.turing.codex_icon import render_codex_icon
from ._vendor.turing.dashboard_chart import render_call_chart

ASSETS = Path(__file__).parent / "assets"
FONTS = ASSETS / "fonts"


def color(value):
    return tuple(int(v) for v in value.split(","))


@lru_cache(maxsize=32)
def font(name, size):
    return ImageFont.truetype(str(FONTS / name), size)


class DashboardRenderer:
    def __init__(self, reduced_motion=False):
        self.theme = yaml.safe_load((ASSETS / "theme/theme.yaml").read_text(encoding="utf-8"))
        self.background = Image.open(ASSETS / "theme/background.png").convert("RGB")
        self.reduced_motion = reduced_motion
        # Erase the baked-in strokes inside the orange disc, preserving its outer edge.
        self.balance_background = self.background.crop((0, 0, 376, 288))
        clean = self.balance_background.resize((1128, 864), Image.Resampling.BICUBIC)
        ImageDraw.Draw(clean).ellipse((-120, -282, 1086, 828), fill=(255, 151, 116))
        self.balance_background = clean.resize((376, 288), Image.Resampling.LANCZOS)
        self._snapshot = None
        self.base = None
        self.icon = None

    def _text(self, image, value, style):
        # Match the original DisplayText anchor and fixed-box clipping behavior.
        layer = (Image.new("RGBA", image.size) if image.mode == "RGBA"
                 else self.background.copy())
        draw = ImageDraw.Draw(layer)
        x, y = style["X"], style["Y"]
        width, height = style.get("WIDTH", 0), style.get("HEIGHT", 0)
        anchor = style.get("ANCHOR", "la")
        face = font(style["FONT"], style["FONT_SIZE"])
        if width and height:
            right, bottom = x + width, y + height
            tx = right if anchor.startswith("r") else (x + width // 2
                                                       if anchor.startswith("m") else x)
            ty = bottom if anchor.endswith("b") else (y + height // 2
                                                       if anchor.endswith("m") else y)
            box = (x, y, right, bottom)
        else:
            tx, ty = x, y
            box = draw.textbbox((tx, ty), value, font=face, anchor=anchor)
        draw.text((tx, ty), value, font=face, fill=color(style["FONT_COLOR"]), anchor=anchor)
        box = tuple(int(v) for v in box)
        image.paste(layer.crop(box), box[:2])

    def _prepare(self, snapshot):
        self.base = self.background.copy()
        self.balance_text = Image.new("RGBA", (376, 288))
        for name, style in self.theme["static_text"].items():
            target = self.balance_text if name == "BALANCE" else self.base
            self._text(target, style["TEXT"], style)
        stats = self.theme["STATS"]["CUSTOM"]
        costs = sorted((c["cost"] for c in snapshot["calls"] or []), reverse=True)[:3]

        def amount(key, digits=2):
            value = snapshot[key]
            return "--" if value is None else f"¥{value:,.{digits}f}"

        values = {
            "NewApiRmbBalance": amount("balance"),
            "NewApiTodaySpend": amount("spend"),
            "NewApiTodayAverage": amount("average", 4) + " / call",
            "NewApiTopCallCosts": "\n".join(
                f"{i + 1}  ¥{costs[i]:.4f}" if i < len(costs) else f"{i + 1}  --"
                for i in range(3)),
        }
        for name, value in values.items():
            target = self.balance_text if name == "NewApiRmbBalance" else self.base
            self._text(target, value, stats[name]["TEXT"])
        chart = stats["NewApiCallCosts"]["CALL_CHART"]
        self.base.paste(render_call_chart(snapshot["calls"], chart, FONTS),
                        (chart["X"], chart["Y"]))
        self.panel = stats["CodexState"]["STATE_ICON"]
        self.icon = render_codex_icon(snapshot["codex"].get("state", "idle"), self.panel,
                                      FONTS, snapshot["codex"], now=0)
        self.motion_mask = Image.new("L", (self.icon.width, 232), 255)
        mask_draw = ImageDraw.Draw(self.motion_mask)
        for y in range(192, 232):
            mask_draw.line((0, y, self.icon.width, y), fill=round(255 * (231 - y) / 39))
        # The original layout has space at the top right for status without hiding metrics.
        statuses = [snapshot.get(k, "") for k in
                    ("account_status", "daily_status", "chart_status", "codex_status")]
        errors = list(dict.fromkeys(s for s in statuses if s))
        label = "DEMO / SAMPLE ACCOUNT DATA" if snapshot.get("demo") else ""
        if errors:
            label = "STALE / " + " | ".join(errors)
        if label:
            label = label[:105]
            draw = ImageDraw.Draw(self.base)
            draw.text((422, 5), label, font=font("roboto-mono/RobotoMono-Regular.ttf", 12),
                      fill=(100, 59, 37), anchor="lt")

    def _balance(self, now):
        # Supersample the thin strokes so slow rotation stays smooth at panel resolution.
        lines = Image.new("L", (1128, 864))
        draw = ImageDraw.Draw(lines)
        for bounds, period, start, sweep, width in (
            ((-36, -90, 356, 270), 16, -100, 285, 1),
            ((-24, -78, 344, 258), -12, 12, 145, 3),
        ):
            angle = start + (now / period % 1) * 360
            draw.arc(tuple(v * 3 for v in bounds), angle, angle + sweep,
                     fill=255, width=width * 3)
        circle = self.balance_background.copy()
        lines = lines.resize(circle.size, Image.Resampling.LANCZOS)
        circle.paste((29, 37, 39), (0, 0, circle.width, circle.height), lines)
        circle.paste(self.balance_text, (0, 0), self.balance_text)
        return circle

    def frame(self, snapshot, now=0):
        if snapshot != self._snapshot:
            self._prepare(snapshot)
            self._snapshot = deepcopy(snapshot)
        frame = self.base.copy()
        frame.paste(self._balance(0 if self.reduced_motion else now), (0, 0))
        icon = self.icon
        if not self.reduced_motion:
            # Transform only the illustration; state words and token/cost figures stay fixed.
            state = snapshot["codex"].get("state", "idle")
            period, amplitude = {"idle": (5, 1.5), "working": (2.4, 4),
                                 "done": (1.8, 7)}.get(state, (5, 1.5))
            wave = math.sin(math.tau * now / period)
            scale = 1 + amplitude / 300 * wave
            cx, cy = 344, 76
            moved = icon.transform(icon.size, Image.Transform.AFFINE,
                                   (1 / scale, 0, cx * (1 - 1 / scale),
                                    0, 1 / scale, cy * (1 - 1 / scale) + amplitude * wave),
                                   Image.Resampling.BICUBIC,
                                   fillcolor=color(self.panel["BACKGROUND_COLOR"]))
            icon = icon.copy()
            # Fade back to the stationary artwork before its state label. Transforming
            # the full source avoids an empty strip along the moving crop's lower edge.
            icon.paste(moved.crop((0, 0, icon.width, 232)), (0, 0), self.motion_mask)
        frame.paste(icon, (self.panel["X"], self.panel["Y"]))
        return frame


def physical_frame(frame, y_offset=18, rotate=0):
    """Map logical content into the visible strip of Libre Panel's 480-row framebuffer."""
    output = Image.new("RGB", (1920, 480), frame.getpixel((1919, 479)))
    visible = frame.crop((0, 0, 1920, 480 - y_offset))
    if rotate == 180:
        visible = visible.transpose(Image.Transpose.ROTATE_180)
    output.paste(visible, (0, y_offset))
    return output
