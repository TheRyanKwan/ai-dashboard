# Adapted from turing-smart-screen-python; see THIRD_PARTY.md.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Version-sensitive local Codex transcript adapter. Never reads prompt content into state."""

import json
import math
import os
import time
from collections import OrderedDict
from pathlib import Path


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _cost(counts, settings):
    if counts is None:
        return None
    models = settings.get("CODEX_MODEL_RATES", {})
    if not isinstance(models, dict):
        return None
    cost = 0.0
    for model, usage in counts.items():
        if not any(usage):
            continue
        rates = models.get(model, models.get("*")) if model else None
        if not isinstance(rates, dict):
            return None
        for tokens, key in zip(usage, ("INPUT", "CACHE_READ", "OUTPUT", "CACHE_WRITE")):
            rate = rates.get(key, rates.get("CACHED_INPUT") if key == "CACHE_READ" else None)
            if key == "CACHE_WRITE" and tokens == 0 and rate is None:
                continue  # Older configurations had only three rates.
            if (not isinstance(rate, (int, float)) or isinstance(rate, bool) or
                    not math.isfinite(rate) or rate < 0):
                return None
            cost += tokens / 1_000_000 * rate
    return cost if math.isfinite(cost) else None


class TranscriptUsage:
    """Tail complete JSONL records, rebuilding state after replacement or truncation."""

    def __init__(self):
        self._reset()

    def _reset(self):
        self.identity = None
        self.offset = 0
        self.modified = None
        self.checkpoint = b""
        self.sessions = set()
        self.total = None
        self.reliable = True
        self.turn = None
        self.baseline = None
        self.invalid_turn = False
        self.completed = False
        self.last_task = None
        self.model = None
        self.components = None
        self.cost_counts = {}
        self.cost_reliable = True
        self.cost_baseline = None
        self.invalid_cost_turn = False
        self.last_task_counts = None

    def read(self, path, session, settings=None):
        unknown = {"session_tokens": None, "last_task_tokens": None}
        if settings is not None:
            unknown.update(session_cost=None, last_task_cost=None)
            unknown["cost_currency"] = settings.get("CODEX_COST_CURRENCY", "USD")
        try:
            with Path(path).open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                stream.seek(max(0, self.offset - len(self.checkpoint)))
                unchanged = stream.read(len(self.checkpoint)) == self.checkpoint
                if (identity != self.identity or stat.st_size < self.offset or
                        not unchanged or
                        stat.st_size == self.offset and self.modified != stat.st_mtime_ns):
                    self._reset()
                self.identity = identity
                stream.seek(self.offset)
                while True:
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break  # A writer may still be appending this record; retry next refresh.
                    self.offset = stream.tell()
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(record, dict):
                        self._record(record)
                stream.seek(max(0, self.offset - 128))
                self.checkpoint = stream.read(min(128, self.offset))
                self.modified = stat.st_mtime_ns
        except OSError:
            return unknown
        # A supplied path or filename alone is insufficient proof of session ownership.
        if not session or session not in self.sessions:
            return unknown
        result = {"session_tokens": self.total if self.reliable else None,
                  "last_task_tokens": self.last_task}
        if settings is not None:
            result["cost_currency"] = settings.get("CODEX_COST_CURRENCY", "USD")
            result.update(session_cost=_cost(self.cost_counts, settings)
                          if self.cost_reliable and self.components is not None else None,
                          last_task_cost=_cost(self.last_task_counts, settings))
        return result

    def _record(self, record):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if record.get("type") == "session_meta":
            if isinstance(payload.get("id"), str):
                self.sessions.add(payload["id"])
            return
        if record.get("type") == "turn_context":
            self.model = payload.get("model") if isinstance(payload.get("model"), str) else None
            return
        if record.get("type") != "event_msg":
            return
        kind = payload.get("type")
        if kind == "task_started":
            turn = payload.get("turn_id")
            if not isinstance(turn, str) or not turn or turn == self.turn:
                return
            self.turn = turn
            self.baseline = self.total
            self.invalid_turn = False
            self.completed = False
            self.cost_baseline = dict(self.cost_counts) if self.components is not None else None
            self.invalid_cost_turn = False
        elif kind == "turn_aborted":
            if payload.get("turn_id") in (None, self.turn):
                self.turn = None
                self.completed = False
        elif kind == "task_complete":
            if self.turn and payload.get("turn_id") == self.turn:
                self.completed = True
                self._complete()
        elif kind == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                return
            usage = info.get("total_token_usage")
            self._record_cost(usage, info.get("last_token_usage"))
            reported_total = _count(usage.get("total_tokens")) if isinstance(usage, dict) else None
            cached = _count(usage.get("cached_input_tokens", 0)) if isinstance(usage, dict) else None
            if reported_total is None or cached is None or cached > reported_total:
                return
            # Match /status: cached input is excluded; output already includes reasoning.
            total = reported_total - cached
            last = info.get("last_token_usage")
            last = _count(last.get("total_tokens")) if isinstance(last, dict) else None
            if self.total is not None and total < self.total:
                # Reset/compaction semantics vary by Codex version. Do not invent a lifetime total.
                self.reliable = False
                self.invalid_turn = True
            if self.total is None and reported_total == last and self.turn:
                self.baseline = 0  # Proven first request, rather than an assumed zero baseline.
            self.total = total
            if self.completed:
                self._complete()  # Completion notification may precede the final usage flush.

    def _complete(self):
        self.last_task = (self.total - self.baseline
                          if not self.invalid_turn and self.total is not None and
                          self.baseline is not None and self.total >= self.baseline else None)
        self.last_task_counts = (
            {model: tuple(value - base for value, base in zip(
                counts, self.cost_baseline.get(model, (0, 0, 0, 0))))
             for model, counts in self.cost_counts.items()}
            if not self.invalid_cost_turn and self.cost_baseline is not None else None)

    def _record_cost(self, usage, last):
        components = None
        if isinstance(usage, dict):
            values = tuple(_count(usage.get(key)) for key in
                           ("input_tokens", "cached_input_tokens", "output_tokens"))
            writes = _count(usage.get("cache_write_input_tokens", 0))
            if (all(value is not None for value in values) and writes is not None and
                    values[1] + writes <= values[0]):
                components = (values[0] - values[1] - writes, values[1], values[2], writes)
        if components is None:
            self.cost_reliable = False
            self.invalid_cost_turn = True
            self.components = None
            return
        previous = self.components
        self.components = components
        if previous is None:
            # Historical usage cannot safely be assigned to the current model.
            first = isinstance(last, dict) and all(
                usage.get(key) == last.get(key) for key in
                ("input_tokens", "cached_input_tokens", "output_tokens"))
            first = first and usage.get("cache_write_input_tokens", 0) == last.get("cache_write_input_tokens", 0)
            if not first:
                self.cost_reliable = False
                self.invalid_cost_turn = True
                return
            previous = (0, 0, 0, 0)
            if self.turn and self.cost_baseline is None:
                self.cost_baseline = dict(self.cost_counts)
        delta = tuple(value - old for value, old in zip(components, previous))
        if any(value < 0 for value in delta):
            self.cost_reliable = False
            self.invalid_cost_turn = True
            return
        if any(delta):
            old = self.cost_counts.get(self.model, (0, 0, 0, 0))
            self.cost_counts[self.model] = tuple(a + b for a, b in zip(old, delta))


class CodexUsageCache:
    """Bounded per-session readers; discover files only when hooks omit their path."""

    def __init__(self):
        self.readers = OrderedDict()

    def read(self, session, transcript, settings):
        unknown = {"session_tokens": None, "last_task_tokens": None,
                   "session_cost": None, "last_task_cost": None,
                   "cost_currency": settings.get("CODEX_COST_CURRENCY", "USD")}
        if not isinstance(session, str) or not session:
            return unknown
        directory = settings.get("CODEX_SESSIONS_DIR") or str(
            Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sessions")
        key = (session, str(transcript or ""), str(directory))
        entry = self.readers.pop(key, None)
        if entry is None:
            entry = {"reader": TranscriptUsage(), "path": None, "next_search": 0}
        self.readers[key] = entry
        while len(self.readers) > 8:
            self.readers.popitem(last=False)
        if isinstance(transcript, str) and transcript:
            entry["path"] = Path(transcript).expanduser()
        elif time.monotonic() >= entry["next_search"] and (
                entry["path"] is None or not entry["path"].is_file()):
            entry["next_search"] = time.monotonic() + 5
            # Treat the session ID as a filename fragment, never a path or glob expression.
            if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in session):
                return unknown
            try:
                candidates = Path(directory).expanduser().glob("**/rollout-*-" + session + ".jsonl")
                entry["path"] = max(candidates, key=lambda path: path.stat().st_mtime_ns, default=None)
            except OSError:
                return unknown
        if entry["path"] is None:
            return unknown
        return entry["reader"].read(entry["path"], session, settings)


usage_cache = CodexUsageCache()
