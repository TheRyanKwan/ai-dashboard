# SPDX-License-Identifier: GPL-3.0-or-later
import http.client
import io
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from PIL import Image

from turzx_dashboard.cli import main
from turzx_dashboard.config import Settings
from turzx_dashboard.runtime import run
from turzx_dashboard.web import BrowserPreview


def stream_picture(response):
    assert response.readline() == b"--frame\r\n"
    assert response.readline() == b"Content-Type: image/jpeg\r\n"
    length = int(response.readline().split(b":")[1])
    assert response.readline() == b"\r\n"
    picture = response.read(length)
    assert response.read(2) == b"\r\n"
    return picture


def test_http_page_snapshots_and_multiple_stream_viewers():
    with BrowserPreview(port=0) as preview:
        root = f"http://127.0.0.1:{preview.port}"
        with urlopen(root) as response:
            page = response.read()
            assert response.status == 200
            assert b"/stream.mjpg" in page and b"Pause animation" in page
            assert b"prefers-reduced-motion" in page
            assert response.headers["Cache-Control"] == "no-store"
        preview.publish(Image.new("RGB", (1920, 480), "red"))
        with urlopen(root + "/screencap.png?fresh=1") as response:
            image = Image.open(io.BytesIO(response.read()))
            assert image.size == (1920, 480)
            assert image.getpixel((0, 0)) == (255, 0, 0)
        connections = [http.client.HTTPConnection("127.0.0.1", preview.port, timeout=5)
                       for _ in range(2)]
        try:
            responses = []
            for connection in connections:
                connection.request("GET", "/stream.mjpg")
                response = connection.getresponse()
                assert response.status == 200
                assert "multipart/x-mixed-replace" in response.headers["Content-Type"]
                responses.append(response)
            first = [stream_picture(response) for response in responses]
            preview.publish(Image.new("RGB", (1920, 480), "blue"))
            for response, previous in zip(responses, first, strict=True):
                current = stream_picture(response)
                assert current != previous
                assert Image.open(io.BytesIO(current)).size == (1920, 480)
            with urlopen(root + "/frame.png") as response:
                assert Image.open(io.BytesIO(response.read())).getpixel((0, 0)) == (0, 0, 255)
            with pytest.raises(HTTPError) as error:
                urlopen(root + "/../dashboard.local.yaml")
            assert error.value.code == 404
        finally:
            for response in responses:
                response.close()
            for connection in connections:
                connection.close()
        port = preview.port
    # Shutdown frees the listening socket even when streaming clients were connected.
    with BrowserPreview(port=port):
        pass


def test_browser_receives_animated_runtime_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_EVENT_FILE", str(tmp_path / "event.json"))
    settings = Settings(dashboard={"DEMO": True}, fps=15)
    stop = threading.Event()
    with BrowserPreview(port=0) as preview:
        thread = threading.Thread(target=run, args=(settings,), kwargs=dict(
            simulate=True, output=tmp_path / "frame.png", stop=stop,
            frame_sink=preview.publish))
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", preview.port, timeout=5)
        try:
            connection.request("GET", "/stream.mjpg")
            response = connection.getresponse()
            first = stream_picture(response)
            changed = False
            for _ in range(6):
                changed |= stream_picture(response) != first
            assert changed
            assert preview.sequence >= 7
            assert (tmp_path / "frame.png").exists()
            response.close()
        finally:
            connection.close()
            stop.set()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_serve_explicit_demo_is_offline_and_never_opens_usb(tmp_path, monkeypatch):
    from turzx_dashboard._vendor.turing import ai_dashboard

    def unexpected(*args, **kwargs):
        pytest.fail("Browser demo must not contact account servers or USB")

    monkeypatch.setattr(ai_dashboard.requests, "get", unexpected)
    monkeypatch.setattr("turzx_dashboard.runtime.create_display", unexpected)
    monkeypatch.setenv("CODEX_EVENT_FILE", str(tmp_path / "event.json"))
    output = tmp_path / "frame.png"
    assert main(["serve", "--demo", "--port", "0", "--duration", ".3",
                 "--output", str(output)]) == 0
    assert output.exists()


def test_stream_waits_for_first_frame_and_stops_cleanly():
    with BrowserPreview(port=0) as preview:
        connection = http.client.HTTPConnection("127.0.0.1", preview.port, timeout=5)
        connection.request("GET", "/stream.mjpg")
        response = connection.getresponse()
        publisher = threading.Timer(.1, lambda: preview.publish(Image.new("RGB", (1920, 480))))
        publisher.start()
        assert Image.open(io.BytesIO(stream_picture(response))).size == (1920, 480)
        publisher.join()
        response.close()
        connection.close()
