import json
from datetime import datetime, timedelta, timezone

from turzx_dashboard._vendor.turing.ai_dashboard import (
    codex_snapshot,
    codex_state,
    write_codex_event,
)
from turzx_dashboard.data import DashboardData


def append(path, kind, *, turn="first", timestamp=None):
    record = {"timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
              "type": "event_msg", "payload": {"type": kind, "turn_id": turn}}
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")


def transcript(tmp_path, session="one"):
    path = tmp_path / f"rollout-date-{session}.jsonl"
    path.write_text(json.dumps({"type": "session_meta",
                                "payload": {"id": session, "cwd": "/projects/example"}}) + "\n")
    return path


def test_poll_detects_work_and_completion_without_hooks(tmp_path):
    path = transcript(tmp_path)
    source = DashboardData({"CODEX_SESSIONS_DIR": str(tmp_path)})
    append(path, "task_started")
    source.poll_codex()
    snapshot = source.snapshot()["codex"]
    assert snapshot["state"] == "working"
    assert snapshot["session_id"] == "one"
    assert snapshot["project"] == "example"
    assert codex_state(source.settings) == "working"
    append(path, "task_complete")
    source.poll_codex()
    assert source.snapshot()["codex"]["state"] == "done"
    assert codex_snapshot(dict(source.settings, CODEX_DONE_SECONDS=0))["state"] == "idle"


def test_abort_ignores_late_completion_and_new_turn_recovers(tmp_path):
    path = transcript(tmp_path)
    settings = {"CODEX_SESSIONS_DIR": str(tmp_path)}
    append(path, "task_started")
    assert codex_snapshot(settings)["state"] == "working"
    append(path, "turn_aborted")
    append(path, "task_complete")
    assert codex_snapshot(settings)["state"] == "idle"
    append(path, "task_started", turn="second")
    append(path, "task_complete", turn="first")
    assert codex_snapshot(settings)["state"] == "working"


def test_newest_lifecycle_wins_across_hooks_and_transcript(tmp_path):
    path = transcript(tmp_path)
    event = tmp_path / "event.json"
    settings = {"CODEX_SESSIONS_DIR": str(tmp_path), "CODEX_EVENT_FILE": str(event)}
    old = datetime.now(timezone.utc) - timedelta(minutes=1)
    append(path, "task_started", timestamp=old)
    write_codex_event({"type": "agent-turn-complete", "thread-id": "one"}, event)
    assert codex_snapshot(settings)["state"] == "done"
    append(path, "task_started", turn="second")
    assert codex_snapshot(settings)["state"] == "working"
    write_codex_event({"hook_event_name": "UserPromptSubmit", "session_id": "one",
                       "turn_id": "second"}, event)
    append(path, "task_complete", turn="second")
    assert codex_snapshot(settings)["state"] == "done"


def test_working_session_selection_and_pin(tmp_path):
    one = transcript(tmp_path)
    two = transcript(tmp_path, "two")
    append(one, "task_started")
    append(two, "task_started")
    append(two, "task_complete")
    settings = {"CODEX_SESSIONS_DIR": str(tmp_path)}
    assert codex_snapshot(settings)["session_id"] == "one"
    assert codex_snapshot(dict(settings, CODEX_SESSION_ID="two"))["state"] == "done"
    assert codex_snapshot(dict(settings, CODEX_SESSION_ID="missing"))["state"] == "idle"


def test_default_codex_home_and_partial_record(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    directory = tmp_path / "sessions"
    directory.mkdir()
    path = transcript(directory)
    append(path, "task_started")
    assert codex_snapshot({})["state"] == "working"
    with path.open("a") as stream:
        stream.write('{"type":"event_msg","payload":{"type":"task_complete","turn_id":"first"}')
    assert codex_snapshot({})["state"] == "working"
    with path.open("a") as stream:
        stream.write(',"timestamp":"' + datetime.now(timezone.utc).isoformat() + '"}\n')
    assert codex_snapshot({})["state"] == "done"


def test_reader_recovers_after_transcript_replacement(tmp_path):
    path = transcript(tmp_path)
    settings = {"CODEX_SESSIONS_DIR": str(tmp_path)}
    append(path, "task_started")
    assert codex_snapshot(settings)["state"] == "working"
    path.unlink()
    transcript(tmp_path)
    append(path, "task_started", turn="replacement")
    append(path, "turn_aborted", turn="replacement")
    assert codex_snapshot(settings)["state"] == "idle"
