# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from turzx_dashboard.cli import main


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("explicit_path", [False, True])
def test_serve_respects_demo_false_in_configuration(tmp_path, monkeypatch, wrapped, explicit_path):
    config = tmp_path / "dashboard.local.yaml"
    config.write_text("dashboard:\n  DEMO: false\n" if wrapped else "DEMO: false\n",
                      encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    selected = []

    def capture(settings, **kwargs):
        selected.append(settings.dashboard["DEMO"])
        return 1

    monkeypatch.setattr("turzx_dashboard.runtime.run", capture)
    options = ["--config", str(config)] if explicit_path else []
    assert main(["serve", *options, "--port", "0"]) == 0
    assert selected == [False]


@pytest.mark.parametrize("configured,flags,expected", [
    (True, [], True),
    (False, ["--demo"], True),
    (True, ["--live"], False),
    (False, ["--live"], False),
])
def test_serve_mode_flags_override_configuration(
        tmp_path, monkeypatch, configured, flags, expected):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dashboard.local.yaml").write_text(f"DEMO: {str(configured).lower()}\n",
                                                 encoding="utf-8")
    selected = []

    def capture(settings, **kwargs):
        selected.append(settings.dashboard["DEMO"])
        return 1

    monkeypatch.setattr("turzx_dashboard.runtime.run", capture)
    assert main(["serve", *flags, "--port", "0"]) == 0
    assert selected == [expected]
