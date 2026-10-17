# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
import yaml

from turzx_dashboard._vendor.turing import new_api_setup
from turzx_dashboard.config import load_settings


@pytest.fixture
def settings(monkeypatch):
    for key in ("NEW_API_URL", "NEW_API_COOKIE", "NEW_API_USER_ID", "NEW_API_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    return dict(NEW_API_URL="https://gateway.example", NEW_API_COOKIE="session=test-cookie",
                NEW_API_USER_ID="42")


def response(data):
    return Mock(status_code=200, json=lambda: {"success": True, "data": data})


@pytest.mark.parametrize("wrapped", [False, True])
def test_script_creates_token_and_checks_account(tmp_path, settings, wrapped):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization"),
                          self.headers.get("Cookie"), self.headers.get("New-Api-User")))
            data = ("generated-test-token" if self.path == "/api/user/token" else
                    dict(id=42, quota=100, used_quota=20, request_count=2))
            body = json.dumps(dict(success=True, data=data)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        settings.update(NEW_API_URL=f"http://127.0.0.1:{server.server_port}", DEMO=True,
                        CODEX_DONE_SECONDS=45)
        original = dict(dashboard=settings, display=dict(brightness=75)) if wrapped else settings
        path = tmp_path / ("settings.yaml" if wrapped else "dashboard.local.yaml")
        path.write_text(yaml.safe_dump(original))
        script = Path(__file__).resolve().parents[1] / "check-new-api.py"
        command = [sys.executable, str(script)]
        if wrapped:
            command += ["--config", str(path)]
        created = subprocess.run(command + ["--create-token"], cwd=tmp_path, env=os.environ,
                                 capture_output=True, text=True, timeout=10)
        assert created.returncode == 0, created.stdout + created.stderr
        loaded = load_settings(path).dashboard
        assert loaded["NEW_API_ACCESS_TOKEN"] == "generated-test-token"
        assert loaded["NEW_API_AUTH_MODE"] == "token"
        assert loaded["DEMO"] is False
        assert loaded["CODEX_DONE_SECONDS"] == 45
        if wrapped:
            assert yaml.safe_load(path.read_text())["display"] == dict(brightness=75)
        checked = subprocess.run(command, cwd=tmp_path, env=os.environ,
                                 capture_output=True, text=True, timeout=10)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert "Connected; account fields validated" in checked.stdout
        assert [call[0] for call in calls] == ["/api/user/self", "/api/user/token",
                                              "/api/user/self", "/api/user/self"]
        assert calls[1][1:] == (None, "session=test-cookie", "42")
        assert calls[2][1:] == ("Bearer generated-test-token", None, "42")
        assert "generated-test-token" not in created.stdout + checked.stdout
        assert "session=test-cookie" not in created.stdout + checked.stdout
        assert list(tmp_path.iterdir()) == [path]
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_rotation_timeout_is_never_retried(tmp_path, settings, monkeypatch):
    get = Mock(side_effect=[response({"id": 42}), requests.Timeout("secret-cookie")])
    monkeypatch.setattr(new_api_setup.requests, "get", get)
    path = tmp_path / "settings.yaml"
    with pytest.raises(ValueError, match="may already have rotated") as error:
        new_api_setup.create_management_token(settings, path)
    assert "secret-cookie" not in str(error.value)
    assert get.call_count == 2
    assert not path.exists()
    assert all(call.kwargs["allow_redirects"] is False for call in get.call_args_list)


def test_verification_failure_keeps_generated_token(tmp_path, settings, monkeypatch):
    get = Mock(side_effect=[response({"id": 42}), response("generated-test-token"),
                            Mock(status_code=401)])
    monkeypatch.setattr(new_api_setup.requests, "get", get)
    path = tmp_path / "settings.yaml"
    with pytest.raises(ValueError, match="saved locally"):
        new_api_setup.create_management_token(settings, path)
    assert load_settings(path).dashboard["NEW_API_ACCESS_TOKEN"] == "generated-test-token"
    assert get.call_count == 3


def test_wrong_account_stops_before_rotation(tmp_path, settings, monkeypatch):
    get = Mock(return_value=response({"id": 999}))
    monkeypatch.setattr(new_api_setup.requests, "get", get)
    with pytest.raises(ValueError, match="does not match"):
        new_api_setup.create_management_token(settings, tmp_path / "settings.yaml")
    get.assert_called_once()


@pytest.mark.parametrize("contents", ["[]", "dashboard: []", "TOKEN: [secret-cookie"])
def test_invalid_config_does_not_rotate(tmp_path, settings, monkeypatch, contents):
    get = Mock()
    monkeypatch.setattr(new_api_setup.requests, "get", get)
    path = tmp_path / "settings.yaml"
    path.write_text(contents)
    with pytest.raises(ValueError) as error:
        new_api_setup.create_management_token(settings, path)
    assert "secret-cookie" not in str(error.value)
    get.assert_not_called()
