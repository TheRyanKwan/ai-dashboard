# SPDX-License-Identifier: GPL-3.0-or-later
"""Command line only: run, preview, doctor, and the Codex event bridge."""

import argparse
import json
import logging
import math
import subprocess
import sys
from pathlib import Path

from .config import load_settings, validate


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return result


def parser():
    root = argparse.ArgumentParser(description="AI-Dashboard for the TURZX 9.2-inch USB screen")
    root.add_argument("--version", action="version", version="turzx-dashboard 0.1.0")
    commands = root.add_subparsers(dest="command", required=True)
    for command in ("run", "preview", "doctor", "serve"):
        p = commands.add_parser(command)
        p.add_argument("--config", type=Path, help="YAML file; defaults to dashboard.local.yaml")
        p.add_argument("--ffmpeg", help="FFmpeg executable; otherwise search PATH")
        if command == "doctor":
            continue
        mode = p.add_mutually_exclusive_group()
        mode.add_argument("--demo", action="store_true", default=None,
                          help="sample account data, no network requests")
        mode.add_argument("--live", action="store_false", dest="demo",
                          help="read configured account and local Codex data")
        p.add_argument("--fps", type=int, help="frames per second (1–50)")
        p.add_argument("--reduced-motion", action="store_true", default=None)
        p.add_argument("--state", choices=("idle", "working", "done"),
                       help="override Codex state for a preview or hardware test")
        if command == "preview":
            p.add_argument("--output", type=Path, default=Path("output/preview.png"),
                           help="PNG still or animated GIF; offline demo unless --live")
            p.add_argument("--seconds", type=positive, default=4,
                           help="GIF duration, at most 10 seconds (default: 4)")
        elif command == "serve":
            p.add_argument("--port", type=int, default=5678, help="HTTP port (default: 5678)")
            p.add_argument("--host", default="127.0.0.1", help="listen address (default: loopback)")
            p.add_argument("--output", type=Path, default=Path("output/frame.png"))
            p.add_argument("--duration", type=positive, help="stop after this many seconds")
        else:
            p.add_argument("--simulate", action="store_true", help="write PNG; do not access USB")
            p.add_argument("--output", type=Path, default=Path("output/frame.png"))
            p.add_argument("--duration", type=positive, help="stop after this many seconds")
            p.add_argument("--clip", help="screen's existing standby clip name, for video startup")
            p.add_argument("--brightness", type=int, help="0–100")
            p.add_argument("--still", action="store_true",
                           help="PNG transport, no FFmpeg/clip needed")
            p.add_argument("--y-offset", type=int, help="hidden top strip compensation, 0–18")
            p.add_argument("--rotate", type=int, choices=(0, 180))
    p = commands.add_parser("notify", help="Codex notification bridge; never opens the screen")
    p.add_argument("--event-file", help="same path as dashboard.CODEX_EVENT_FILE")
    p.add_argument("--hook", action="store_true", help="read lifecycle hook JSON from stdin")
    p.add_argument("--test", action="store_true", help="write a sample completion")
    p.add_argument("payload", nargs="?", help="Codex notification JSON")
    return root


def preview(settings, args):
    from .data import DashboardData, demo_codex
    from .output import save_png
    from .render import DashboardRenderer

    suffix = args.output.suffix.lower()
    if suffix not in (".png", ".gif"):
        raise ValueError("preview output must end in .png or .gif")
    if suffix == ".gif" and args.seconds > 10:
        raise ValueError("GIF previews are limited to 10 seconds")
    source = DashboardData(settings.dashboard)
    snapshot = source.refresh()
    snapshot["demo"] = settings.dashboard["DEMO"]
    if args.state is not None or snapshot["demo"]:
        snapshot["codex"] = (demo_codex(args.state or "working") if snapshot["demo"] else
                             dict(snapshot["codex"], state=args.state))
    renderer = DashboardRenderer(settings.reduced_motion)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".png":
        save_png(renderer.frame(snapshot), args.output)
    else:
        fps = min(settings.fps, 15)
        frames = [renderer.frame(snapshot, i / fps).quantize(colors=128)
                  for i in range(max(2, math.ceil(args.seconds * fps)))]
        frames[0].save(args.output, save_all=True, append_images=frames[1:],
                       duration=round(1000 / fps), loop=0, optimize=False)
    for key in ("account_status", "daily_status", "chart_status"):
        if snapshot[key]:
            print(snapshot[key], file=sys.stderr)
    print(args.output.resolve())
    return 0


def doctor(settings):
    from ._vendor.libre_panel.devices.base import DeviceError
    from ._vendor.libre_panel.devices.h264 import find_ffmpeg

    print(f"Python: {sys.version.split()[0]} ({sys.platform})")
    ok = True
    try:
        executable = find_ffmpeg(settings.ffmpeg)
        check = subprocess.run([executable, "-hide_banner", "-encoders"],
                               capture_output=True, text=True, timeout=10)
        encoder_ok = check.returncode == 0 and "libx264" in check.stdout
        print(f"FFmpeg: {'libx264 available' if encoder_ok else 'libx264 missing'}")
        ok = ok and encoder_ok
    except (DeviceError, OSError, subprocess.TimeoutExpired):
        print("FFmpeg: missing or unusable; install FFmpeg with libx264")
        ok = False
    try:
        import usb.backend.libusb1
        import usb.core

        from ._vendor.libre_panel.devices.turzx_usb import _backend

        backend = _backend() or usb.backend.libusb1.get_backend()
        if backend is None:
            raise DeviceError("libusb missing")
        device = usb.core.find(idVendor=0x1CBE, idProduct=0x0092, backend=backend)
        print("libusb: available")
        print(f"TURZX 9.2 (1cbe:0092): {'found' if device is not None else 'not connected'}")
        ok = ok and device is not None
    except (ImportError, DeviceError, OSError, ValueError):
        print("USB: unavailable; install turzx-dashboard[usb] and system libusb")
        ok = False
    try:
        from Crypto.Cipher import DES  # noqa: F401
        print("USB command encryption: available")
    except ImportError:
        print("USB command encryption: missing; install turzx-dashboard[usb]")
        ok = False
    print(f"Standby clip: {'configured' if settings.clip else 'set display.clip or run --clip'}")
    return 0 if ok and settings.clip else 1


def notify(args):
    from ._vendor.turing.ai_dashboard import event_path, write_codex_event

    if args.test:
        payload = {"type": "agent-turn-complete", "cwd": "turzx-dashboard"}
    elif args.hook:
        payload = json.load(sys.stdin)
    elif args.payload:
        payload = json.loads(args.payload)
    else:
        raise ValueError("Supply a notification JSON payload, --hook, or --test")
    settings = {"CODEX_EVENT_FILE": args.event_file} if args.event_file else {}
    write_codex_event(payload, event_path(settings))
    return 0


def main(argv=None):
    root = parser()
    args = root.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "notify":
            return notify(args)
        settings = load_settings(args.config)
        for key in ("fps", "ffmpeg", "reduced_motion", "clip", "brightness", "y_offset", "rotate"):
            value = getattr(args, key, None)
            if value is not None:
                setattr(settings, key, value)
        if args.command == "preview":
            settings.dashboard["DEMO"] = args.demo is not False
        elif getattr(args, "demo", None) is not None:
            settings.dashboard["DEMO"] = args.demo
        validate(settings)
        if args.command == "preview":
            return preview(settings, args)
        if args.command == "doctor":
            return doctor(settings)
        if args.command == "serve":
            from .runtime import run
            from .web import BrowserPreview
            if not 0 <= args.port <= 65535:
                raise ValueError("port must be between 0 and 65535")
            with BrowserPreview(args.host, args.port) as browser:
                frames = run(settings, simulate=True, output=args.output, duration=args.duration,
                             state=args.state, frame_sink=browser.publish)
            return 0 if frames else 1
        if not args.simulate and not args.still:
            if not settings.clip:
                raise ValueError("Video needs --clip with the screen's existing standby clip name; "
                                 "use --simulate to preview or --still for PNG output")
            from ._vendor.libre_panel.devices.h264 import find_ffmpeg
            settings.ffmpeg = find_ffmpeg(settings.ffmpeg)
        from .runtime import run
        frames = run(settings, simulate=args.simulate, output=args.output, duration=args.duration,
                     state=args.state, still=args.still)
        return 0 if frames else 1
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        # Configuration validation messages contain key names, never credential values.
        print(f"error: {exc}", file=sys.stderr)
        return 1
