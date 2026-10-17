# Adapted from turing-smart-screen-python; see THIRD_PARTY.md.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Account polling and Codex events, independent of LCD and sensor scheduling."""

import hashlib
import json
import math
import os
import tempfile
import textwrap
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests


DEFAULT_EVENT_FILE = "~/.cache/turzx-dashboard/codex-event.json"


def event_path(settings):
    return Path(os.environ.get("CODEX_EVENT_FILE") or
                settings.get("CODEX_EVENT_FILE", DEFAULT_EVENT_FILE)).expanduser()


def clean_text(value, limit=80):
    return " ".join(str(value).split())[:limit]


def quota_to_rmb(value, settings):
    quota = float(settings.get("QUOTA_PER_RMB", 500000))
    if not math.isfinite(quota) or quota <= 0:
        raise ValueError("Set a positive quota-per-RMB scale")
    return value / quota


def call_usage(item):
    """Read optional usage without letting malformed metadata hide valid costs."""
    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    prompt = count(item.get("prompt_tokens"))
    completion = count(item.get("completion_tokens"))
    duration = item.get("use_time")
    if (isinstance(duration, bool) or not isinstance(duration, (int, float)) or
            not math.isfinite(duration) or duration < 0):
        duration = None
    other = item.get("other", {})
    if isinstance(other, str):
        try:
            other = json.loads(other)
        except (ValueError, TypeError):
            other = {}
    if not isinstance(other, dict):
        other = {}
    details = item.get("prompt_tokens_details") or other.get("prompt_tokens_details") or {}
    cached = count(other.get("cache_tokens"))
    if cached is None and isinstance(details, dict):
        cached = count(details.get("cached_tokens"))
    # Unknown is distinct from a reported cache miss. Reject inconsistent counts.
    if prompt is None or cached is not None and cached > prompt:
        cached = None
    return {"duration": duration, "input_tokens": prompt, "tokens": prompt + completion if prompt is not None and completion is not None else None,
            "cached": cached, "hit_rate": cached / prompt if cached is not None and prompt else
            (0.0 if cached == 0 and prompt == 0 else None)}


class DailyCallsCache:
    """Paginate today's consumption logs; retain each call, including zero-cost calls."""

    def __init__(self, latest_only=False):
        self.latest_only = latest_only
        self.total_calls = 0
        self.calls = None
        self.usage = {}
        self.day = None
        self.updated = None
        self.next_poll = 0
        self.error = "Collecting calls"

    def refresh(self, settings):
        now = datetime.now(timezone(timedelta(hours=float(settings.get("DAY_UTC_OFFSET", 8)))))
        if self.day != now.date():
            self.day = now.date()
            self.calls = None
            self.usage = {}
            self.total_calls = 0
            self.updated = None
            self.next_poll = 0
        if time.monotonic() < self.next_poll:
            return
        interval = settings.get("CHART_POLL_SECONDS", 2) if self.latest_only else settings.get("POLL_SECONDS", 20)
        self.next_poll = time.monotonic() + max(1, float(interval))
        if settings.get("DEMO", False):
            self.calls = [(i, 600 + (i * 7919 % 24000)) for i in range(60)]
            self.usage = {}
            for identity, _ in self.calls:
                prompt = 1000 + identity * 7919 % 28000
                cached = int(prompt * (identity % 6) / 5)
                self.usage[identity] = {"duration": 1 + (identity * 17 % 240) / 10, "input_tokens": prompt, "tokens": prompt + 200 + identity * 337 % 3000,
                                        "cached": cached, "hit_rate": cached / prompt}
            self.total_calls = len(self.calls)
            self.updated = time.time()
            self.error = ""
            return
        url = os.environ.get("NEW_API_URL") or settings.get("NEW_API_URL", "")
        mode = str(settings.get("NEW_API_AUTH_MODE", "token")).strip().lower()
        token = str(os.environ.get(settings.get("NEW_API_TOKEN_ENV", "NEW_API_ACCESS_TOKEN")) or
                    settings.get("NEW_API_ACCESS_TOKEN", "")).strip()
        cookie = os.environ.get("NEW_API_COOKIE") or settings.get("NEW_API_COOKIE", "")
        if not url or not (cookie if mode == "cookie" else token):
            self.error = "Set account credentials"
            return
        if mode not in ("token", "cookie") or not url.startswith(("http://", "https://")):
            self.error = "Check URL and auth mode"
            return
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        headers = {"Cookie": str(cookie)} if mode == "cookie" else {"Authorization": "Bearer " + token}
        user_id = os.environ.get(settings.get("NEW_API_USER_ID_ENV", "NEW_API_USER_ID")) or settings.get("NEW_API_USER_ID", "")
        if user_id != "" and user_id is not None:
            headers["New-Api-User"] = str(user_id)
        start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        end = int(now.timestamp())
        calls = {}
        usage = {}
        deadline = time.monotonic() + float(settings.get("LOG_TIMEOUT_SECONDS", 20))
        try:
            for page in range(1, int(settings.get("LOG_PAGE_LIMIT", 100)) + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.error = "Call log collection timed out"
                    return
                response = requests.get(url.rstrip("/") + "/api/log/self", headers=headers,
                                        params={"type": 2, "p": page, "page_size": 100,
                                                "start_timestamp": start, "end_timestamp": end,
                                                "token_name": "", "model_name": "", "group": ""},
                                        timeout=min(remaining, float(settings.get("TIMEOUT_SECONDS", 5))),
                                        allow_redirects=False)
                if response.status_code != 200:
                    self.error = f"Call logs HTTP {response.status_code}"
                    return
                body = response.json()
                if not isinstance(body, dict) or body.get("success") is not True:
                    raise ValueError()
                data = body.get("data")
                if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                    raise ValueError()
                total = data.get("total")
                if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                    raise ValueError()
                for item in data["items"]:
                    if not isinstance(item, dict) or item.get("type") != 2:
                        raise ValueError()
                    identity, created, cost = item.get("id"), item.get("created_at"), item.get("quota")
                    if not isinstance(identity, int) or isinstance(identity, bool):
                        raise ValueError()
                    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                           for v in (created, cost)) or not start <= created <= end or cost < 0:
                        raise ValueError()
                    calls[identity] = (created, cost)
                    usage[identity] = call_usage(item)
                if self.latest_only or page * 100 >= total:
                    if len(calls) != (min(100, total) if self.latest_only else total):
                        raise ValueError()
                    self.calls = [(key, value[1]) for key, value in
                                  sorted(calls.items(), key=lambda pair: (pair[1][0], pair[0]))]
                    self.usage = usage
                    self.total_calls = total
                    self.updated = time.time()
                    self.error = ""
                    return
                if not data["items"]:
                    raise ValueError()
            self.error = "Call log page limit reached"
        except requests.RequestException:
            self.error = "Call logs connection failed"
        except (ValueError, TypeError, KeyError):
            self.error = "Incomplete or invalid call logs"


class AccountCache:
    """One shared poll per interval, including failed attempts. Keep last good data."""

    def __init__(self):
        self.data = None
        self.updated = None
        self.next_poll = 0
        self.error = "Not connected"
        self.balance_history = deque(maxlen=60)

    def refresh(self, settings):
        now = time.monotonic()
        if now < self.next_poll:
            return
        self.next_poll = now + max(1, float(settings.get("POLL_SECONDS", 20)))
        if settings.get("DEMO", False):
            self.data = {"quota": 1250000, "used_quota": 375000, "request_count": 1284}
            self.updated = time.time()
            self.error = ""
            if not self.balance_history:
                # Illustrative history only; live mode starts with an empty trace.
                self.balance_history.extend(1250000 + ((59 - i) // 3) * 9000 +
                                            (80000 if i >= 22 else 0) +
                                            (50000 if i >= 43 else 0) - 130000 for i in range(60))
            else:
                self.balance_history.append(self.data["quota"])
            return

        url = os.environ.get("NEW_API_URL") or settings.get("NEW_API_URL", "")
        token = (os.environ.get(settings.get("NEW_API_TOKEN_ENV", "NEW_API_ACCESS_TOKEN")) or
                 settings.get("NEW_API_ACCESS_TOKEN", ""))
        mode = str(settings.get("NEW_API_AUTH_MODE", "token")).strip().lower()
        cookie = os.environ.get("NEW_API_COOKIE") or settings.get("NEW_API_COOKIE", "")
        if mode not in ("token", "cookie"):
            self.error = "Auth mode must be token or cookie"
            return
        if not url or not (cookie if mode == "cookie" else token):
            self.error = ("Set New API URL and session cookie" if mode == "cookie" else
                          "Set New API URL and account token")
            return
        if not url.startswith(("https://", "http://")):
            self.error = "New API URL must use http(s)"
            return
        # Match all-api-hub's explicit modes: never mix an invalid Bearer with a
        # valid cookie session, which can cause the server to reject the request.
        if mode == "cookie":
            headers = {"Cookie": str(cookie).strip()}
        else:
            token = str(token).strip()
            if token.lower().startswith("bearer "):
                token = token[7:].strip()
            headers = {"Authorization": "Bearer " + token}
        user_id = (os.environ.get(settings.get("NEW_API_USER_ID_ENV", "NEW_API_USER_ID")) or
                   settings.get("NEW_API_USER_ID", ""))
        if user_id != "" and user_id is not None:
            headers["New-Api-User"] = str(user_id).strip()
        endpoint = settings.get("NEW_API_ENDPOINT", "/api/user/self")
        try:
            # Do not follow redirects with account authentication headers.
            response = requests.get(url.rstrip("/") + "/" + endpoint.lstrip("/"),
                                    headers=headers, timeout=float(settings.get("TIMEOUT_SECONDS", 5)),
                                    allow_redirects=False)
            response.raise_for_status()
            if response.status_code != 200:
                self.error = "Unexpected API response"
                return
            body = response.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                self.error = "API rejected account request"
                return
            data = body.get("data")
            if not isinstance(data, dict):
                raise ValueError("Missing account data")
            validated = {}
            for field in ("quota", "used_quota", "request_count"):
                value = data[field]
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
                    raise ValueError("Invalid account value")
                validated[field] = value
            self.data = validated
            self.updated = time.time()
            self.error = ""
            self.balance_history.append(validated["quota"])
        except requests.HTTPError as exc:
            self.error = "API HTTP " + str(exc.response.status_code)
        except requests.Timeout:
            self.error = "API request timed out"
        except requests.exceptions.SSLError:
            self.error = "API TLS certificate error"
        except requests.ConnectionError:
            self.error = "API connection failed"
        except requests.RequestException:
            self.error = "API connection failed"
        except (ValueError, KeyError, TypeError):
            self.error = "Unexpected account data"

    def amount(self, field, settings):
        self.refresh(settings)
        if self.data is None:
            return None
        divisor = float(settings.get("QUOTA_PER_UNIT", 1))
        if not math.isfinite(divisor) or divisor <= 0:
            return None
        return self.data[field] / divisor

    def status(self, settings):
        self.refresh(settings)
        prefix = "DEMO | " if settings.get("DEMO", False) else ""
        if self.updated is None:
            return prefix + self.error
        age = max(0, int(time.time() - self.updated))
        if self.error:
            return prefix + f"Stale {age}s | {self.error}"
        return prefix + f"Updated {age}s ago"


def write_codex_event(payload, path):
    """Accept completion notifications or lifecycle hooks, never writing to the LCD."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a notification object")
    hook = payload.get("hook_event_name")
    if payload.get("type") == "agent-turn-complete":
        state = "done"
    elif hook == "UserPromptSubmit":
        state = "working"
    elif hook in ("Interrupt", "SessionEnd"):
        state = "idle"
    else:
        return False
    session = payload.get("session_id") or payload.get("thread-id")
    turn = payload.get("turn_id") or payload.get("turn-id")
    if session is not None and (not isinstance(session, str) or not session):
        raise ValueError("Expected a session ID")
    if turn is not None and (not isinstance(turn, str) or not turn):
        raise ValueError("Expected a turn ID")
    now = datetime.now(timezone.utc).isoformat()
    event = {
        "state": state, "updated_at": now, "session_id": session, "turn_id": turn,
        "project": clean_text(str(payload.get("cwd", "")).replace("\\", "/").rstrip("/").split("/")[-1]),
        "message": clean_text(payload.get("last-assistant-message") or "Turn complete", 160)
                   if state == "done" else "",
    }
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        event["transcript_path"] = transcript
    if state == "done":
        event["completed_at"] = now
    path = Path(path).expanduser()
    if session:
        # Independent records prevent one session's completion from hiding another's work.
        record = Path(str(path) + ".sessions") / (hashlib.sha256(session.encode()).hexdigest() + ".json")
        try:
            previous = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        if state != "working" and hook != "SessionEnd":
            if turn and previous.get("turn_id") and turn != previous["turn_id"]:
                return False  # A late terminal event must not replace a newer turn.
            if state == "done" and previous.get("state") == "idle" and previous.get("turn_id") == turn:
                return False  # An interrupted/closed turn must stay idle.
        if hook == "SessionEnd" and previous.get("state") == "done":
            return False  # Closing Codex after completion must not shorten the done window.
        if "transcript_path" not in event and isinstance(previous.get("transcript_path"), str):
            event["transcript_path"] = previous["transcript_path"]
        if state != "working" and not turn:
            event["turn_id"] = previous.get("turn_id")
        _write_codex_record(event, record)
    # Retain the original file for existing completion-text themes and integrations.
    _write_codex_record(event, path)
    return True


def _write_codex_record(event, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".codex-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(event, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def codex_state(settings, now=None):
    """Use the same lifecycle sources and expiry as the dashboard snapshot."""
    return codex_snapshot(settings, now=now)["state"]


def codex_snapshot(settings, now=None):
    """Select one conversation so the icon and token counters describe the same work."""
    from turzx_dashboard._vendor.turing.codex_activity import activity_cache
    from turzx_dashboard._vendor.turing.codex_usage import usage_cache

    now = now or datetime.now(timezone.utc)
    path = event_path(settings)
    selected = settings.get("CODEX_SESSION_ID")
    events = activity_cache.read(settings)
    for record in list(Path(str(path) + ".sessions").glob("*.json")) + [path]:
        try:
            event = json.loads(record.read_text(encoding="utf-8"))
            if not isinstance(event, dict) or record == path and event.get("session_id"):
                continue
            events.append(event)
        except (OSError, ValueError):
            continue
    # Reconcile each conversation before prioritizing working sessions. Otherwise
    # an older start could hide a newer completion from the other event source.
    latest = {}
    for index, event in enumerate(events):
        try:
            updated = datetime.fromisoformat(event.get("updated_at") or event.get("completed_at"))
            if updated.tzinfo is None:
                continue
            key = event.get("session_id") or ("anonymous", index)
            if key not in latest or updated >= latest[key][0]:
                latest[key] = (updated, event)
        except (TypeError, ValueError):
            continue
    records = []
    for updated, event in latest.values():
        try:
            if selected and event.get("session_id") != selected:
                continue
            state = event.get("state", "done" if "completed_at" in event else "idle")
            if state not in ("working", "done", "idle"):
                continue
            # Use the same expiry rules as the legacy aggregate state sensor.
            if state == "done":
                try:
                    duration = float(settings.get("CODEX_DONE_SECONDS", 30))
                    if not math.isfinite(duration) or duration < 0:
                        duration = 30
                except (TypeError, ValueError):
                    duration = 30
                age = (now - datetime.fromisoformat(event["completed_at"])).total_seconds()
                if not 0 <= age < duration:
                    state = "idle"
            records.append(({"working": 2, "done": 1, "idle": 0}[state], updated, event, state))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if not records:
        return {"state": "idle", "session_tokens": None, "last_task_tokens": None, "session_cost": None, "last_task_cost": None, "project": ""}
    _, _, event, state = max(records, key=lambda item: (item[0], item[1]))
    usage = usage_cache.read(event.get("session_id"), event.get("transcript_path"), settings)
    return dict(usage, state=state, session_id=event.get("session_id"),
                project=clean_text(event.get("project") or "Codex", 24))


def codex_text(settings, part=None):
    try:
        event = json.loads(event_path(settings).read_text(encoding="utf-8"))
        if event.get("state") in ("working", "idle"):
            parts = {"status": event["state"].title(),
                     "project": clean_text(event.get("project") or "Codex", 27), "message": ""}
            return (parts[part] or " ") if part else "\n".join(value for value in parts.values() if value)
        completed = datetime.fromisoformat(event["completed_at"])
        age = max(0, int((datetime.now(timezone.utc) - completed).total_seconds()))
        elapsed = f"{age}s" if age < 60 else f"{age // 60}m"
        project = clean_text(event.get("project") or "Codex", 27)
        message = clean_text(event.get("message", "Turn complete"), 96)
        # Explicit line lengths keep notification content inside the panel.
        lines = textwrap.wrap(message, width=40 if part else 32)[:3]
        parts = {"status": f"Turn complete | {elapsed} ago", "project": project,
                 "message": "\n".join(lines)}
    except FileNotFoundError:
        parts = {"status": "Waiting for Codex", "project": "No completion received", "message": ""}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        parts = {"status": "Cannot read Codex event", "project": "", "message": ""}
    if part:
        # LCD text must contain at least one character, even when clearing a field.
        return parts[part] or " "
    return "\n".join(value for value in parts.values() if value)
