# Adapted from turing-smart-screen-python; see THIRD_PARTY.md.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Per-call telemetry rendering; no sensor polling or device access."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def render_call_chart(calls, theme, fonts_dir):
    """Align independently scaled cost/token bars with cache hit ratios per call."""
    if theme.get("STYLE") == "ribbon":
        return _render_ribbons(calls, theme, fonts_dir)

    def color(key, default):
        value = theme.get(key, default)
        return tuple(int(v) for v in value.split(',')) if isinstance(value, str) else tuple(value)

    width, height = int(theme.get("WIDTH", 901)), int(theme.get("HEIGHT", 108))
    background = color("BACKGROUND_COLOR", (12, 25, 30))
    muted = color("TEXT_COLOR", (143, 166, 166))
    amber = color("COST_COLOR", (244, 178, 99))
    blue = color("TIME_COLOR", theme.get("TOKEN_COLOR", (115, 191, 222)))
    purple = color("INPUT_COLOR", (172, 158, 230))
    green = color("CACHE_COLOR", (185, 231, 202))
    track = tuple(min(255, v + 16) for v in background)
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(Path(fonts_dir) / theme.get("FONT", "roboto-mono/RobotoMono-Regular.ttf")),
                              int(theme.get("FONT_SIZE", 11)))
    detail_font = ImageFont.truetype(str(Path(fonts_dir) / theme.get("FONT", "roboto-mono/RobotoMono-Regular.ttf")),
                                     int(theme.get("DETAIL_FONT_SIZE", 17)))
    label_font = ImageFont.truetype(str(Path(fonts_dir) / theme.get("FONT", "roboto-mono/RobotoMono-Regular.ttf")),
                                    int(theme.get("LABEL_FONT_SIZE", 17)))

    def y(value):
        return round(value * height / 144) + int(theme.get("PLOT_Y_OFFSET", 0))

    available = calls is not None
    calls = (calls or [])[-60:]
    left = 140
    step = (width - left) / 60
    cost_max = max((call['cost'] for call in calls), default=0)
    duration_max = max((call.get('duration') or 0 for call in calls), default=0)
    input_max = max((call.get('input_tokens') or 0 for call in calls), default=0)
    for center, label, shade in ((12.5, "COST / RMB", amber),
                                 (43.5, "INPUT TOKENS", purple),
                                 (74.5, "TIME / SEC", blue),
                                 (101, "CACHE HIT %", green)):
        draw.text((0, y(center)), label, fill=shade, font=label_font, anchor="lm")
    # Each lane uses its own units; fixed 60 slots keep columns stable as calls arrive.
    for index in range(60):
        x = round(left + index * step)
        right = round(left + (index + 1) * step) - 3
        for top, bottom in ((0, 25), (31, 56), (62, 87), (94, 108)):
            draw.rectangle((x, y(top), right, y(bottom)), fill=track)
        if index >= len(calls):
            continue
        call = calls[index]
        for value, maximum, bottom, shade in ((call['cost'], cost_max, 25, amber),
                                               (call.get('input_tokens'), input_max, 56, purple),
                                               (call.get('duration'), duration_max, 87, blue)):
            if value is None:
                draw.line((x, y(bottom - 2), right, y(bottom - 7)), fill=muted)
            else:
                bar_height = round(24 * height / 144 * value / maximum) if maximum else 0
                draw.rectangle((x, y(bottom) - bar_height, right, y(bottom)), fill=shade)
        rate = call.get('hit_rate')
        if rate is None:
            draw.line((x, y(106), right, y(96)), fill=muted)
        elif rate > 0:
            draw.rectangle((x, y(94), min(right, x + round((right - x) * rate)), y(108)), fill=green)
        else:
            draw.line((x, y(108), right, y(108)), fill=green)
    if calls:
        latest = calls[-1]
        input_tokens = f"{latest['input_tokens']:,}" if latest.get('input_tokens') is not None else "--"
        duration = f"{latest['duration']:.1f}s" if latest.get('duration') is not None else "--"
        cached = f"{latest['cached']:,}" if latest.get('cached') is not None else "--"
        hit = f"{latest['hit_rate']:.0%}" if latest.get('hit_rate') is not None else "--"
        detail = f"LATEST ¥{latest['cost']:.4f} / IN {input_tokens} / TIME {duration} / CACHED {cached} / HIT {hit}"
        x = round(left + (len(calls) - 1) * step)
        draw.line((x, y(112), x + max(1, round(step) - 3), y(112)), fill=green, width=2)
    else:
        detail = "NO CALLS TODAY" if available else "WAITING FOR CALLS"
    while draw.textlength(detail, font=detail_font) > width and detail_font.size > font.size:
        detail_font = detail_font.font_variant(size=detail_font.size - 1)
    draw.text((0, height - 3), detail, fill=muted, font=detail_font, anchor="lb")
    return image


def _render_ribbons(calls, theme, fonts_dir):
    """Four staggered strips with aligned calls and the newest values at the right."""
    def color(key):
        value = theme[key]
        return tuple(int(v) for v in value.split(',')) if isinstance(value, str) else tuple(value)

    width, height = int(theme["WIDTH"]), int(theme["HEIGHT"])
    image = Image.new("RGB", (width, height), color("SURROUND_COLOR"))
    draw = ImageDraw.Draw(image)
    font_path = str(Path(fonts_dir) / theme["FONT"])
    label_font = ImageFont.truetype(font_path, int(theme.get("LABEL_FONT_SIZE", 17)))
    value_font = ImageFont.truetype(font_path, int(theme.get("DETAIL_FONT_SIZE", 22)))
    available = calls is not None
    calls = (calls or [])[-60:]
    latest = calls[-1] if calls else {}

    def number(key, spec):
        value = latest.get(key)
        return format(value, spec) if value is not None else "—"

    rows = (("cost", "cost", "COST_COLOR", "¥" + number("cost", ".4f")),
            ("input", "input_tokens", "INPUT_COLOR", number("input_tokens", ",")),
            ("time", "duration", "TIME_COLOR", number("duration", ".1f") + " s"),
            ("cache", "hit_rate", "CACHE_COLOR",
             number("cached", ",") + " cached / " + number("hit_rate", ".0%")))
    lane_height = height / 4
    left, plot_width = 116, width - 316
    step = plot_width / 60
    for row, (label, key, color_key, detail) in enumerate(rows):
        top = round(row * lane_height)
        baseline = round((row + 1) * lane_height) - 25
        plot_top = top + 19
        shade = color(color_key)
        inset = 13 if row % 2 else 0
        bottom = round((row + 1) * lane_height) - 10
        draw.polygon(((inset + 15, top), (width - 20 + inset, top), (width - 5, top + 15),
                      (width - 16, bottom), (inset, bottom), (inset, top + 15)),
                     fill=color("BACKGROUND_COLOR"))
        draw.polygon(((inset, top + 15), (inset + 5, top + 10),
                      (inset + 5, bottom - 12), (inset, bottom - 7)), fill=shade)
        draw.text((inset + 20, top + 37), label, font=label_font, fill=shade, anchor="lt")
        if calls:
            if key == "hit_rate":
                draw.text((width - 28, top + 22), number("hit_rate", ".0%"),
                          font=value_font, fill=shade, anchor="rt")
                draw.text((width - 28, top + 53), number("cached", ",") + " cached",
                          font=label_font, fill=shade, anchor="rt")
            else:
                draw.text((width - 28, top + 36), detail, font=value_font, fill=shade, anchor="rt")
        elif row == 0:
            message = "no calls today" if available else "waiting for calls"
            draw.text((width - 28, top + 38), message, font=label_font,
                      fill=color("TEXT_COLOR"), anchor="rt")
        maximum = 1 if key == "hit_rate" else max((call.get(key) or 0 for call in calls), default=0)
        draw.line((left, baseline, left + plot_width - 1, baseline), fill=color("TRACK_COLOR"))
        for slot in range(60):
            x = left + round(slot * step)
            right = left + round((slot + 1) * step) - 3
            index = slot - (60 - len(calls))
            if index < 0:
                draw.point((x, baseline - 3), fill=color("TRACK_COLOR"))
                continue
            value = calls[index].get(key)
            if value is None:
                draw.line((x, baseline - 3, right, baseline - 10), fill=color("TEXT_COLOR"), width=2)
            else:
                fraction = min(1, max(0, value / maximum)) if maximum else 0
                bar_height = max(2, round((baseline - plot_top) * fraction))
                draw.rounded_rectangle((x, baseline - bar_height, right, baseline), radius=2, fill=shade)
        if calls:
            draw.line((left + round(59 * step), baseline + 5,
                       left + plot_width - 3, baseline + 5), fill=shade, width=2)
    return image
