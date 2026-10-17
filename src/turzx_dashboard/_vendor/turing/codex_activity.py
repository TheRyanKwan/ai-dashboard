# SPDX-License-Identifier: GPL-3.0-or-later
"""Discover lifecycle events when Codex integrations do not emit start hooks."""

import threading

from .codex_usage import TranscriptUsage, _sessions_directory


class CodexActivityCache:
    def __init__(self):
        self.readers = {}
        self.lock = threading.Lock()

    def read(self, settings):
        # Separate polling consumers must not advance the same reader concurrently.
        with self.lock:
            return self._read(settings)

    def _read(self, settings):
        directory = _sessions_directory(settings)
        events = []
        try:
            paths = list(directory.glob("**/rollout-*.jsonl"))
            paths.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        except OSError:
            return events
        # Keep incremental readers bounded even with years of saved conversations.
        selected = settings.get("CODEX_SESSION_ID")
        if selected:
            paths = [path for path in paths if path.name.endswith(f"-{selected}.jsonl")]
        paths = paths[:128]
        self.readers = {path: reader for path, reader in self.readers.items() if path in paths}
        for path in paths:
            reader = self.readers.setdefault(path, TranscriptUsage())
            # The metadata, rather than a filename, supplies the conversation ID.
            reader.read(path, None, settings)
            if len(reader.sessions) != 1 or reader.activity is None:
                continue
            session = next(iter(reader.sessions))
            if settings.get("CODEX_SESSION_ID") and session != settings["CODEX_SESSION_ID"]:
                continue
            events.append(dict(reader.activity, session_id=session, project=reader.project,
                               transcript_path=str(path)))
        return events


activity_cache = CodexActivityCache()
