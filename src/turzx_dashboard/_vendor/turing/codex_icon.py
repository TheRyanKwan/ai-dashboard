# Adapted from turing-smart-screen-python; see THIRD_PARTY.md.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Codex state artwork, independent of sensors and display drivers."""

from pathlib import Path
import math
import time

from PIL import Image, ImageDraw, ImageFont


def render_codex_icon(state, theme, fonts_dir, snapshot=None, now=None):
    """Compose icon and optional token counters, repainting the full panel interior."""
    if state not in ("idle", "working", "done"):
        state = "idle"
    if theme.get("LAYOUT") == "signal":
        return _render_signal(state, theme, fonts_dir, snapshot or {}, now)

    def color(key, default):
        value = theme.get(key, default)
        return tuple(int(v) for v in value.split(',')) if isinstance(value, str) else tuple(value)

    background = color("BACKGROUND_COLOR", (18, 61, 57))
    if state == "done":
        background = color("DONE_BACKGROUND_COLOR", background)
    shade = color(state.upper() + "_COLOR", {"idle": (146, 188, 167),
                                           "working": (244, 178, 99),
                                           "done": (185, 231, 202)}[state])
    width, height = int(theme.get("WIDTH", 635)), int(theme.get("HEIGHT", 325))
    image = Image.new("RGB", (width, height), background)
    # Draw at double resolution for smooth outlines without platform icon fonts.
    artwork = Image.new("RGB", (480, 480), background)
    draw = ImageDraw.Draw(artwork)
    draw.rounded_rectangle((42, 68, 438, 358), radius=32, outline=shade, width=12)
    draw.line((50, 114, 430, 114), fill=shade, width=6)
    for x in (72, 98, 124):
        draw.ellipse((x, 86, x + 10, 96), fill=shade)
    draw.line((240, 358, 240, 402), fill=shade, width=12)
    draw.line((170, 408, 310, 408), fill=shade, width=12)
    if state == "idle":
        for x in (130, 285):
            draw.arc((x, 173, x + 65, 224), 0, 180, fill=shade, width=10)
        draw.line((213, 278, 267, 278), fill=shade, width=10)
    elif state == "working":
        draw.line((115, 176, 170, 221, 115, 266), fill=shade, width=12, joint="curve")
        for index, bar_height in enumerate((36, 70, 110)):
            x = 224 + index * 43
            draw.rounded_rectangle((x, 278 - bar_height, x + 23, 278), radius=5, fill=shade)
    else:
        draw.line((142, 231, 213, 293, 342, 170), fill=shade, width=20, joint="curve")
    show_tokens = theme.get("SHOW_TOKENS", False)
    icon_width = min(width, int(theme.get("ICON_WIDTH", 275))) if show_tokens else width
    size = min(240, icon_width - 20, height - 70)
    artwork = artwork.resize((size, size), Image.Resampling.LANCZOS)
    top = max(0, (height - size - 45) // 2)
    image.paste(artwork, ((icon_width - size) // 2, top))
    font = ImageFont.truetype(str(Path(fonts_dir) / theme.get("FONT", "roboto/Roboto-Bold.ttf")),
                              int(theme.get("FONT_SIZE", 26)))
    ImageDraw.Draw(image).text((icon_width // 2, top + size + 8), state.upper(),
                               font=font, fill=shade, anchor="mt")
    if show_tokens:
        snapshot = snapshot or {}
        draw = ImageDraw.Draw(image)
        x, y = int(theme.get("TOKEN_X", 300)), int(theme.get("TOKEN_Y", 64))
        label_font = ImageFont.truetype(str(Path(fonts_dir) / theme.get("FONT", "roboto/Roboto-Bold.ttf")),
                                       int(theme.get("TOKEN_LABEL_SIZE", 17)))
        value_font = ImageFont.truetype(str(Path(fonts_dir) / theme.get("FONT", "roboto/Roboto-Bold.ttf")),
                                       int(theme.get("TOKEN_VALUE_SIZE", 36)))
        rows = [("SESSION TOKENS", "session_tokens"), ("LAST TASK TOKENS", "last_task_tokens")]
        if theme.get("SHOW_COSTS", False):
            rows.extend((("SESSION COST", "session_cost"), ("TASK COST", "last_task_cost")))
        for index, (label, key) in enumerate(rows):
            row_y = y + index * int(theme.get("TOKEN_ROW_HEIGHT", 105))
            draw.text((x, row_y), label, font=label_font, fill=shade, anchor="lt")
            value = (format_cost(snapshot.get(key), snapshot.get("cost_currency", "USD"))
                     if key.endswith("cost") else format_tokens(snapshot.get(key)))
            draw.text((x, row_y + 26), value, font=value_font,
                      fill=color("TOKEN_COLOR", (241, 234, 211)), anchor="lt")
        project = snapshot.get("project", "")
        session = snapshot.get("session_id")
        if session:
            project = project[:16] + " / " + session[:8]
        if project:
            while project and draw.textlength(project, font=label_font) > width - x - 16:
                project = project[:-1]
            draw.text((x, height - 30), project, font=label_font, fill=shade, anchor="lt")
    return image


def _render_signal(state, theme, fonts_dir, snapshot, now=None):
    """An expressive status face above two columns of session and last-task usage."""
    def color(key):
        value = theme[key]
        return tuple(int(v) for v in value.split(',')) if isinstance(value, str) else tuple(value)

    width, height = int(theme["WIDTH"]), int(theme["HEIGHT"])
    scale = 2
    notify = state == "done" and "DONE_BURST_COLOR" in theme
    background = color("BACKGROUND_COLOR")
    phase = 0
    if notify and theme.get("DONE_PULSE_SECONDS", 0) > 0:
        elapsed = time.monotonic() if now is None else now
        phase = (1 - math.cos(math.tau * elapsed / theme["DONE_PULSE_SECONDS"])) / 2
    upbeat = phase >= .5
    image = Image.new("RGB", (width * scale, height * scale), background)
    draw = ImageDraw.Draw(image)
    ink, accent = color("TOKEN_COLOR"), color(state.upper() + "_COLOR")
    foreground = ink

    def font(size, bold=False):
        name = theme["FONT"] if bold else theme.get("LABEL_FONT", theme["FONT"])
        return ImageFont.truetype(str(Path(fonts_dir) / name), size * scale)

    def text(x, y, value, size, shade=foreground, bold=False, max_width=None):
        face = font(size, bold)
        limit = max_width if max_width is not None else width - x - 28
        while draw.textlength(value, font=face) > limit * scale and face.size > 20:
            face = face.font_variant(size=face.size - 1)
        draw.text((x * scale, y * scale), value, font=face, fill=shade, anchor="lt")

    # An oversized, irregular silhouette gives the status its own character.
    # Expressions encode state; this is decoration with a useful job.
    cx, cy = 344 * scale, 76 * scale
    if notify:
        # The mascot becomes a paper rosette. Its breathing silhouette is the signal.
        burst = []
        for step in range(240):
            angle = step * math.tau / 240
            radius = (180 + phase * 24 + 12 * math.cos(12 * angle)) * scale
            burst.append((cx + math.cos(angle) * radius * 1.13,
                          cy + math.sin(angle) * radius))
        draw.polygon(burst, fill=color("DONE_BURST_COLOR"))
        draw.line(burst + burst[:1], fill=ink, width=2 * scale, joint="curve")
        orbit = (168 + phase * 22) * scale
        draw.ellipse((cx - orbit * 1.13, cy - orbit, cx + orbit * 1.13, cy + orbit),
                     outline=ink, width=scale)
    points = []
    for step in range(180):
        angle = step * math.tau / 180
        radius = ((143 + 4 * math.sin(5 * angle + .7)) if notify else
                  (167 + 12 * math.sin(5 * angle + .7))) * scale
        points.append((cx + math.cos(angle) * radius * 1.15,
                       cy + math.sin(angle) * radius))
    if not notify:
        draw.line((153 * scale, 16 * scale, 119 * scale, 3 * scale), fill=ink, width=3 * scale)
        draw.line((153 * scale, 30 * scale, 113 * scale, 38 * scale), fill=ink, width=3 * scale)
    draw.polygon(points, fill=color("FACE_COLOR"))
    draw.line(points + points[:1], fill=ink, width=2 * scale, joint="curve")
    paper = color("BACKGROUND_COLOR")
    for x in (222, 457):
        draw.ellipse(((x - 21) * scale, 115 * scale, (x + 21) * scale, 150 * scale),
                     fill=color("CHEEK_COLOR"))
    if state == "working":
        for x, y in ((273, 49), (399, 37)):
            draw.rounded_rectangle(((x - 30) * scale, y * scale, (x + 30) * scale, (y + 81) * scale),
                                   radius=29 * scale, fill=paper)
            draw.rounded_rectangle(((x - 6) * scale, (y + 26) * scale,
                                    (x + 14) * scale, (y + 67) * scale), radius=10 * scale, fill=ink)
        draw.line((294 * scale, 164 * scale, 315 * scale, 153 * scale,
                   337 * scale, 169 * scale, 359 * scale, 153 * scale, 380 * scale, 161 * scale),
                  fill=ink, width=5 * scale, joint="curve")
    elif state == "done":
        for x in (273, 399):
            draw.arc(((x - 29) * scale, 61 * scale, (x + 29) * scale, 109 * scale),
                     185, 355, fill=ink, width=7 * scale)
        draw.arc((290 * scale, 109 * scale, 387 * scale, 184 * scale),
                 5, 175, fill=ink, width=7 * scale)
        for x, y in ((97 - phase * 12, 108), (566 + phase * 12, 194)):
            size = (12 + phase * 9) * scale
            x, y = x * scale, y * scale
            draw.polygon((x, y - size, x + size * .25, y - size * .25,
                          x + size, y, x + size * .25, y + size * .25,
                          x, y + size, x - size * .25, y + size * .25,
                          x - size, y, x - size * .25, y - size * .25), fill=accent)
    else:
        for x in (273, 399):
            draw.arc(((x - 30) * scale, 54 * scale, (x + 30) * scale, 97 * scale),
                     5, 175, fill=ink, width=6 * scale)
        draw.ellipse((326 * scale, 143 * scale, 352 * scale, 161 * scale), outline=ink, width=4 * scale)
    if notify:
        # A two-pose paper animation reads clearly at the existing two-second refresh.
        # Move only the illustration; usage figures and the DONE word remain legible.
        hero_box = (0, 0, width * scale, 314 * scale)
        hero = image.crop(hero_box)
        draw.rectangle(hero_box, fill=background)
        image.paste(hero, (0, -round(phase * 18 * scale)))
        burst_color = color("DONE_BURST_COLOR")
        ribbon_color, ribbon_text = (burst_color, ink) if upbeat else (ink, background)
        seal_color, seal_mark = (ink, background) if upbeat else (burst_color, ink)
        draw.polygon((20 * scale, 250 * scale, 73 * scale, 240 * scale,
                      82 * scale, 300 * scale, 22 * scale, 308 * scale,
                      36 * scale, 278 * scale), fill=ink if upbeat else burst_color)
        draw.polygon((72 * scale, 240 * scale, 535 * scale, 228 * scale,
                      549 * scale, 288 * scale, 84 * scale, 304 * scale), fill=ribbon_color)
        text(100, 245, "DONE", int(theme.get("DONE_FONT_SIZE", 58)), ribbon_text, True)
        # A check seal balances the word and repeats the round balance motif.
        bx, by, radius = 497 * scale, 265 * scale, 25 * scale
        draw.ellipse((bx - radius, by - radius, bx + radius, by + radius), fill=seal_color)
        draw.line((bx - 12 * scale, by, bx - 3 * scale, by + 9 * scale,
                   bx + 13 * scale, by - 10 * scale), fill=seal_mark, width=4 * scale, joint="curve")
    else:
        text(28, 249, state.upper(), 42, accent, True)
        draw.line((28 * scale, 304 * scale, (width - 30) * scale, 304 * scale), fill=ink, width=scale)
    if theme.get("SHOW_TOKENS", False):
        for x, label, token_key, cost_key in ((28, "session", "session_tokens", "session_cost"),
                                              (width // 2 + 8, "last task", "last_task_tokens", "last_task_cost")):
            text(x, 321, label, 15)
            text(x, 346, format_tokens(snapshot.get(token_key)), 43, foreground, True, width // 2 - 48)
            if theme.get("SHOW_COSTS", False):
                value = format_cost(snapshot.get(cost_key), snapshot.get("cost_currency", "USD"))
                text(x, 400, value, 24, max_width=width // 2 - 48)
    project = snapshot.get("project") or ""
    if project:
        face = font(17)
        while project and draw.textlength(project, font=face) > (width - 56) * scale:
            project = project[:-1]
        text(28, height - 36, project, 15)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def format_cost(value, currency="USD"):
    if (not isinstance(value, (int, float)) or isinstance(value, bool) or
            not math.isfinite(value) or value < 0):
        return "\u2014"
    symbol = {"USD": "$", "CNY": "¥"}.get(currency, str(currency) + " ")
    return f"{symbol}{value:.4f}"


def format_tokens(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "\u2014"
    for scale, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= scale:
            return f"{value / scale:.2f}".rstrip("0").rstrip(".") + suffix
    return str(value)
