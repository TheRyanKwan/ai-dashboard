# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose the original layout into complete, independently animated frames."""

import math
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import yaml
from PIL import Image, ImageChops, ImageDraw, ImageFont

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

    @staticmethod
    def _paper_mask(image, shade):
        # Only untouched paper can receive trails; illustration edges stay intact.
        difference = ImageChops.difference(image, Image.new("RGB", image.size, shade))
        channels = difference.split()
        difference = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
        return difference.point([255] + [0] * 255)

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

        paper = color(self.panel["BACKGROUND_COLOR"])
        self.meteor_mask = self._paper_mask(self.base, paper)
        mask = ImageDraw.Draw(self.meteor_mask)
        # Reserve complete text boxes, including the spaces between glyphs.
        for style in [*self.theme["static_text"].values(),
                      *(stats[name]["TEXT"] for name in values)]:
            x, y = style["X"], style["Y"]
            if style.get("WIDTH") and style.get("HEIGHT"):
                box = (x, y, x + style["WIDTH"], y + style["HEIGHT"])
            else:
                box = ImageDraw.Draw(self.base).textbbox(
                    (x, y), style["TEXT"], font=font(style["FONT"], style["FONT_SIZE"]),
                    anchor=style.get("ANCHOR", "la"))
            mask.rectangle(box, fill=0)
        mask.rectangle((0, 0, 375, 287), fill=0)
        mask.rectangle((chart["X"], chart["Y"], chart["X"] + chart["WIDTH"] - 1,
                        chart["Y"] + chart["HEIGHT"] - 1), fill=0)
        mask.rectangle((self.panel["X"], self.panel["Y"],
                        self.panel["X"] + self.icon.width - 1,
                        self.panel["Y"] + self.icon.height - 1), fill=0)
        # The status line may contain variable-length error messages.
        mask.rectangle((422, 0, 1309, 20), fill=0)

    def _meteors(self, frame, icon, now):
        trails = Image.new("RGBA", frame.size)
        draw = ImageDraw.Draw(trails)
        palette = ((211, 123, 92), (150, 124, 191), (93, 160, 173))
        # x/y start, travel vector, repeat interval, phase offset. Independent
        # cycles keep the sky varied without mutable particles or random flicker.
        paths = (
            (350, -25, 590, 150, 3.4, .2),
            (1315, -30, 90, 260, 3.7, .25),
            (1950, -20, -100, 250, 4.3, .7),
            (400, 448, 760, 36, 3.1, .6),
            (-100, 422, 460, 100, 3.9, .45),
            (1250, 490, -720, -44, 4.65, 2.4),
            (325, 100, 270, 160, 4.15, 2.2),
            (1320, 10, 520, 260, 5.35, 3.05),
        )
        for index, (x, y, dx, dy, period, phase) in enumerate(paths):
            age = (now + phase) % period
            duration = 2.8
            if age >= duration:
                continue
            progress = age / duration
            fade = min(progress / .15, 1, (1 - progress) / .25)
            head_x, head_y = x + dx * progress, y + dy * progress
            distance = math.hypot(dx, dy)
            ux, uy = dx / distance, dy / distance
            length = 210
            shade = palette[index % len(palette)]
            # Draw from faint tail to bright head, with a soft wider outer trail.
            for step in range(32):
                along = step / 32
                tail = length * (1 - along)
                end = length * (1 - (step + 1) / 32)
                segment = (head_x - ux * tail, head_y - uy * tail,
                           head_x - ux * end, head_y - uy * end)
                alpha = round(180 * along ** 1.6 * fade)
                draw.line(segment, fill=(*shade, alpha // 5), width=11)
                draw.line(segment, fill=(*shade, alpha), width=4)
            draw.ellipse((head_x - 3.5, head_y - 3.5, head_x + 3.5, head_y + 3.5),
                         fill=(*shade, round(220 * fade)))
        mask = self.meteor_mask.copy()
        # Recompute only the moving illustration's paper, so trails pass behind it.
        paper = self._paper_mask(icon.crop((0, 0, icon.width, 232)),
                                 color(self.panel["BACKGROUND_COLOR"]))
        mask.paste(paper, (self.panel["X"], self.panel["Y"]))
        trails.putalpha(ImageChops.multiply(trails.getchannel("A"), mask))
        frame.paste(trails, (0, 0), trails)

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
            if state == "done":
                # A small satisfied tilt, eased back to rest, with a quiet pause.
                progress = min((now % 6) / 1.6, 1)
                lean = .08 * math.sin(math.tau * progress) * math.sin(math.pi * progress) ** 2
                # Lean around the base of the face so its cropped top stays filled.
                moved = icon.transform(icon.size, Image.Transform.AFFINE,
                                       (1, lean, -220 * lean, 0, 1, 0),
                                       Image.Resampling.BICUBIC,
                                       fillcolor=color(self.panel["BACKGROUND_COLOR"]))
            else:
                period, amplitude = {"idle": (5, 1.5), "working": (2.4, 4)}.get(state, (5, 1.5))
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
        if not self.reduced_motion:
            self._meteors(frame, icon, now)
        return frame


def physical_frame(frame, y_offset=18, rotate=0):
    """Map logical content into the visible strip of Libre Panel's 480-row framebuffer."""
    output = Image.new("RGB", (1920, 480), frame.getpixel((1919, 479)))
    visible = frame.crop((0, 0, 1920, 480 - y_offset))
    if rotate == 180:
        visible = visible.transpose(Image.Transpose.ROTATE_180)
    output.paste(visible, (0, y_offset))
    return output
