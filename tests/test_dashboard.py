# SPDX-License-Identifier: GPL-3.0-or-later
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image, ImageChops, ImageStat

from turzx_dashboard import data
from turzx_dashboard.cli import main
from turzx_dashboard.config import Settings, load_settings, validate
from turzx_dashboard.data import DashboardData, demo_codex
from turzx_dashboard.render import DashboardRenderer, physical_frame
from turzx_dashboard.runtime import run


@pytest.fixture(autouse=True)
def isolated_events(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_EVENT_FILE", str(tmp_path / "event.json"))
    for name in ("NEW_API_URL", "NEW_API_ACCESS_TOKEN", "NEW_API_COOKIE", "NEW_API_USER_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sample(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Demo must not contact the network")
    monkeypatch.setattr(data.AccountCache.refresh.__globals__["requests"], "get", no_network)
    source = DashboardData({"DEMO": True})
    snapshot = source.refresh()
    snapshot.update(codex=demo_codex(), demo=True)
    return snapshot


@pytest.mark.parametrize("state", ["idle", "working", "done"])
def test_motion_preserves_metrics_and_layout(sample, state):
    sample["codex"] = demo_codex(state)
    renderer = DashboardRenderer()
    first = renderer.frame(sample, 0)
    second = renderer.frame(sample, .6)
    assert first.size == (1920, 480)
    assert first.crop((0, 0, 1310, 480)).tobytes() == second.crop((0, 0, 1310, 480)).tobytes()
    assert first.crop((1310, 320, 1920, 480)).tobytes() == second.crop(
        (1310, 320, 1920, 480)).tobytes()
    assert first.tobytes() != second.tobytes()
    seam_y = renderer.panel["Y"] + 231
    assert first.crop((0, seam_y, 1920, seam_y + 1)).tobytes() == second.crop(
        (0, seam_y, 1920, seam_y + 1)).tobytes()
    renderer = DashboardRenderer(reduced_motion=True)
    assert renderer.frame(sample, 0).tobytes() == renderer.frame(sample, 1).tobytes()


def test_visible_strip_mapping_and_rotation():
    frame = Image.new("RGB", (1920, 480), "white")
    frame.putpixel((100, 0), (255, 0, 0))
    frame.putpixel((100, 461), (0, 255, 0))
    mapped = physical_frame(frame)
    assert mapped.getpixel((100, 18)) == (255, 0, 0)
    assert mapped.getpixel((100, 479)) == (0, 255, 0)
    inverted = physical_frame(frame, rotate=180)
    assert inverted.getpixel((1819, 479)) == (255, 0, 0)
    assert inverted.getpixel((1819, 18)) == (0, 255, 0)


def test_original_flat_and_wrapped_config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("DEMO: true\nNEW_API_USER_ID: '42'\n", encoding="utf-8")
    assert validate(load_settings(path)).dashboard["DEMO"] is True
    path.write_text("dashboard:\n  QUOTA_PER_RMB: 100\ndisplay:\n  brightness: 25\nfps: 20\n",
                    encoding="utf-8")
    settings = validate(load_settings(path))
    assert (settings.fps, settings.brightness, settings.dashboard["QUOTA_PER_RMB"]) == (20, 25, 100)
    path.write_text("NEW_API_ACCESS_TOKEN: [private-token\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid YAML") as error:
        load_settings(path)
    assert "private-token" not in str(error.value)


@pytest.mark.parametrize("key,value", [("fps", 0), ("fps", 51), ("brightness", 101),
                                      ("y_offset", -1), ("rotate", 90),
                                      ("reduced_motion", "false")])
def test_invalid_settings(key, value):
    settings = Settings()
    setattr(settings, key, value)
    with pytest.raises(ValueError):
        validate(settings)


def test_cli_offline_preview_and_gif(tmp_path, sample):
    output = tmp_path / "preview.png"
    assert main(["preview", "--state", "done", "--output", str(output)]) == 0
    with Image.open(output) as image:
        assert image.size == (1920, 480)
    gif = tmp_path / "preview.gif"
    assert main(["preview", "--seconds", ".5", "--fps", "6", "--output", str(gif)]) == 0
    with Image.open(gif) as image:
        assert image.n_frames == 3
        first = image.convert("RGB")
        image.seek(1)
        assert first.tobytes() != image.convert("RGB").tobytes()


def test_simulator_and_notify_are_cli_only(tmp_path):
    output = tmp_path / "frame.png"
    assert main(["run", "--demo", "--simulate", "--duration", ".4",
                 "--output", str(output)]) == 0
    assert output.is_file()
    assert main(["run", "--demo", "--duration", ".1"]) == 1  # missing clip
    event = tmp_path / "event.json"
    command = [sys.executable, "-m", "turzx_dashboard", "notify", "--event-file", str(event)]
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "one", "turn_id": "turn-1"}
    result = subprocess.run(command + ["--hook"], input=json.dumps(payload), text=True,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    source = DashboardData({"CODEX_EVENT_FILE": str(event)})
    source.poll_codex()
    assert source.snapshot()["codex"]["state"] == "working"
    result = subprocess.run(command + [json.dumps({"type": "agent-turn-complete",
                                                  "thread-id": "one", "turn-id": "turn-1"})],
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    source.poll_codex()
    assert source.snapshot()["codex"]["state"] == "done"


def test_sigterm_stops_simulator_cleanly(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows terminate() ends a process without delivering SIGTERM")
    output = tmp_path / "frame.png"
    process = subprocess.Popen([sys.executable, "-m", "turzx_dashboard", "run", "--demo",
                                "--simulate", "--output", str(output)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert output.exists()
        process.terminate()
        _, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_live_polling_paginated_totals_and_stale_recovery():
    requests = []
    fail = threading.Event()
    timestamp = int(datetime.now(timezone.utc).timestamp())
    rows = [dict(id=i, type=2, created_at=timestamp, quota=i, prompt_tokens=1000,
                 other={"cache_tokens": 500}, use_time=1.5) for i in range(100, -1, -1)]

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import parse_qs, urlparse
            url = urlparse(self.path)
            requests.append((url.path, self.headers.get("Authorization")))
            if fail.is_set():
                self.send_response(503)
                self.end_headers()
                return
            if url.path == "/api/user/self":
                body = dict(quota=1250000, used_quota=100, request_count=101)
            else:
                page = int(parse_qs(url.query)["p"][0])
                body = dict(total=101, items=rows[(page - 1) * 100:page * 100])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(dict(success=True, data=body)).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        source = DashboardData(dict(NEW_API_URL=f"http://127.0.0.1:{server.server_port}",
                                    NEW_API_ACCESS_TOKEN="test-token", QUOTA_PER_RMB=500000))
        snapshot = source.refresh()
        assert snapshot["balance"] == 2.5
        assert snapshot["spend"] == pytest.approx(sum(range(101)) / 500000)
        assert snapshot["average"] == pytest.approx(snapshot["spend"] / 101)
        assert len(snapshot["calls"]) == 60
        assert snapshot["calls"][-1]["cost"] == 100 / 500000
        assert snapshot["calls"][-1]["hit_rate"] == .5
        assert len(requests) == 4  # balance, two total pages, one chart page
        assert all(auth == "Bearer test-token" for _, auth in requests)
        source.refresh()
        assert len(requests) == 4
        fail.set()
        for cache in (source.account, source.daily, source.chart):
            cache.next_poll = 0
        stale = source.refresh()
        assert stale["spend"] == snapshot["spend"] and stale["calls"] == snapshot["calls"]
        assert stale["account_status"] and stale["daily_status"] and stale["chart_status"]
        fail.clear()
        for cache in (source.account, source.daily, source.chart):
            cache.next_poll = 0
        assert not source.refresh()["daily_status"]
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_reconnect_releases_device_and_resumes(tmp_path):
    from turzx_dashboard._vendor.libre_panel.devices.base import DeviceError

    class Display:
        opens = 0
        closes = 0
        shown = 0

        def open(self):
            self.opens += 1
            if self.opens == 1:
                raise DeviceError("temporarily unplugged")

        def close(self):
            self.closes += 1

        def set_brightness(self, value):
            pass

        def describe(self):
            return "test screen"

        def show(self, frame):
            self.shown += 1
            assert frame.size == (1920, 480)

    settings = Settings(dashboard={"DEMO": True})
    display = Display()
    run(settings, display=display, duration=3.3)
    assert display.opens == 2 and display.closes == 2 and display.shown > 0


def test_slow_account_request_does_not_block_frames(tmp_path, monkeypatch):
    output = tmp_path / "frame.png"
    source = DashboardData({"DEMO": True})
    observed = threading.Event()

    def slow_account():
        time.sleep(.65)
        if output.exists():
            observed.set()

    monkeypatch.setattr(source, "poll_account", slow_account)
    frames = run(Settings(dashboard={"DEMO": True}), source=source,
                 simulate=True, output=output, duration=.4)
    assert frames >= 2
    assert observed.is_set()  # frames reached disk before the account request returned


def test_real_h264_encoder_to_simulated_panel(monkeypatch, sample, tmp_path):
    import imageio_ffmpeg
    from fake_panel import FakePanel, transport_for

    from turzx_dashboard._vendor.libre_panel.devices import turzx_usb, turzx_video
    from turzx_dashboard._vendor.libre_panel.devices.h264 import slice_count
    from turzx_dashboard.output import create_display

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    panel = FakePanel()
    monkeypatch.setattr(turzx_usb.UsbTransport, "open",
                        staticmethod(lambda pid=None: transport_for(panel)))
    monkeypatch.setattr(turzx_video, "RESTART", turzx_video._Restart())
    settings = Settings(fps=25, clip="usr/data/standby.h264", ffmpeg=ffmpeg)
    display = create_display(settings)
    renderer = DashboardRenderer()
    source_frames = []
    try:
        display.open()
        for i in range(35):
            frame = physical_frame(renderer.frame(sample, i / 25))
            source_frames.append(frame)
            display.show(frame)
            time.sleep(1 / 25)
        deadline = time.monotonic() + 5
        while len(panel.blocks) < 33 and time.monotonic() < deadline:
            time.sleep(.05)
    finally:
        display.close()
    assert len(panel.blocks) >= 30
    assert panel.overwritten == 0
    assert not {13, 40, 42, 98, 99, 113, 125} & set(panel.commands)
    assert panel.commands[:8] == [10, 110, 111, 112, 14, 102, 15, 17]
    assert panel.commands[-3:] == [123, 15, 102]
    assert all(slice_count(block) == 1 for block in panel.blocks)
    decoded = subprocess.run([ffmpeg, "-loglevel", "error", "-f", "h264", "-i", "-",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             input=panel.stream(), capture_output=True, timeout=30, check=True)
    size = 480 * 1920 * 3
    assert len(decoded.stdout) // size == len(panel.blocks)
    frame = Image.frombytes("RGB", (480, 1920), decoded.stdout[:size])
    difference = ImageChops.difference(
        source_frames[0].transpose(Image.Transpose.ROTATE_270), frame)
    assert max(ImageStat.Stat(difference).mean) < 6
    assert panel.frames[-1].size == (480, 1920)
    (tmp_path / "stream.h264").write_bytes(panel.stream())
