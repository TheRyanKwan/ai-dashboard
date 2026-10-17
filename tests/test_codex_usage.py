# Upstream regression tests; imports relocated. See THIRD_PARTY.md.
# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from turzx_dashboard._vendor.turing.codex_icon import format_cost, format_tokens, render_codex_icon
from turzx_dashboard._vendor.turing.ai_dashboard import codex_snapshot, write_codex_event
from turzx_dashboard._vendor.turing.codex_usage import CodexUsageCache, TranscriptUsage


class CodexUsageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "rollout-date-one.jsonl"
        self.reader = TranscriptUsage()
        self.append({"type": "session_meta", "payload": {"id": "one"}})

    def append(self, record):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    def event(self, kind, **fields):
        self.append({"type": "event_msg", "payload": dict(fields, type=kind)})

    def usage(self, total, last=None):
        self.event("token_count", info={"total_token_usage": {"total_tokens": total},
                                       "last_token_usage": {"total_tokens": last}})

    def read(self):
        return self.reader.read(self.path, "one")

    def test_full_turn_counts_freeze_and_restart_recovery(self):
        self.event("task_started", turn_id="first")
        self.usage(100, 100)
        self.usage(350, 250)
        self.usage(350, 250)  # Duplicate notification, not another inference.
        self.event("task_complete", turn_id="first")
        self.assertEqual(self.read(), {"session_tokens": 350, "last_task_tokens": 350})
        self.event("task_started", turn_id="second")
        self.usage(600, 250)
        self.event("task_started", turn_id="second")  # Duplicate start must not reset baseline.
        self.assertEqual(self.read(), {"session_tokens": 600, "last_task_tokens": 350})
        self.usage(1000, 400)
        self.event("task_complete", turn_id="second")
        expected = {"session_tokens": 1000, "last_task_tokens": 650}
        self.assertEqual(self.read(), expected)
        self.assertEqual(TranscriptUsage().read(self.path, "one"), expected)
        self.assertEqual(self.read(), expected)  # Polling unchanged file does not accumulate again.

    def test_unknown_baseline_then_known_next_turn(self):
        self.event("task_started", turn_id="resumed")
        self.usage(500, 100)  # Earlier usage is not present in this transcript.
        self.event("task_complete", turn_id="resumed")
        self.assertIsNone(self.read()["last_task_tokens"])
        self.event("task_started", turn_id="next")
        self.usage(700, 200)
        self.event("task_complete", turn_id="next")
        self.assertEqual(self.read(), {"session_tokens": 700, "last_task_tokens": 200})

    def test_cached_input_excluded_like_status(self):
        def usage(total, cached, last, last_cached):
            self.event("token_count", info={
                "total_token_usage": {"total_tokens": total, "cached_input_tokens": cached},
                "last_token_usage": {"total_tokens": last, "cached_input_tokens": last_cached}})

        self.event("task_started", turn_id="first")
        usage(19374, 4096, 19374, 4096)
        usage(187415, 148864, 40156, 38784)
        self.event("task_complete", turn_id="first")
        expected = {"session_tokens": 38551, "last_task_tokens": 38551}
        self.assertEqual(self.read(), expected)
        self.assertEqual(TranscriptUsage().read(self.path, "one"), expected)
        self.event("task_started", turn_id="next")
        usage(227891, 152960, 40476, 4096)
        self.assertEqual(self.read(), {"session_tokens": 74931, "last_task_tokens": 38551})
        self.event("task_complete", turn_id="next")
        self.assertEqual(self.read()["last_task_tokens"], 36380)
        usage(228891, 153460, 1000, 500)  # Late usage flush.
        self.assertEqual(self.read(), {"session_tokens": 75431, "last_task_tokens": 36880})

    def test_cached_history_does_not_prove_first_request(self):
        self.event("task_started", turn_id="resumed")
        self.event("token_count", info={
            "total_token_usage": {"total_tokens": 500, "cached_input_tokens": 400},
            "last_token_usage": {"total_tokens": 200, "cached_input_tokens": 100}})
        self.event("task_complete", turn_id="resumed")
        self.assertEqual(self.read(), {"session_tokens": 100, "last_task_tokens": None})

    def test_late_flush_partial_lines_and_invalid_records(self):
        self.event("task_started", turn_id="first")
        self.usage(100, 100)
        self.event("task_complete", turn_id="first")
        self.assertEqual(self.read()["last_task_tokens"], 100)
        with self.path.open("ab") as stream:
            stream.write(b'[]\n{broken}\n{"type":"event_msg","payload":{"type":"token_count","info":')
        self.assertEqual(self.read()["last_task_tokens"], 100)
        with self.path.open("ab") as stream:
            stream.write(b'{"total_token_usage":{"total_tokens":175}}}}\n')
        self.assertEqual(self.read(), {"session_tokens": 175, "last_task_tokens": 175})
        for invalid in (None, True, -3, "200"):
            self.usage(invalid)
        self.event("token_count", info=None)
        self.assertEqual(self.read()["session_tokens"], 175)

    def test_interrupted_turn_keeps_last_completed_count(self):
        self.event("task_started", turn_id="first")
        self.usage(100, 100)
        self.event("task_complete", turn_id="first")
        self.event("task_started", turn_id="aborted")
        self.usage(200, 100)
        self.event("turn_aborted", turn_id="aborted")
        self.usage(250, 50)
        self.event("task_complete", turn_id="aborted")
        self.assertEqual(self.read(), {"session_tokens": 250, "last_task_tokens": 100})

    def test_counter_reset_is_not_reported_as_zero_usage(self):
        self.event("task_started", turn_id="first")
        self.usage(100, 100)
        self.usage(50, 50)
        self.event("task_complete", turn_id="first")
        self.assertEqual(self.read(), {"session_tokens": None, "last_task_tokens": None})
        self.event("task_started", turn_id="next")
        self.usage(125, 75)
        self.event("task_complete", turn_id="next")
        self.assertEqual(self.read(), {"session_tokens": None, "last_task_tokens": 75})

    def test_missing_wrong_session_replacement_and_truncation(self):
        self.usage(100, 100)
        self.assertEqual(self.read()["session_tokens"], 100)
        self.assertIsNone(self.reader.read(self.path, "other")["session_tokens"])
        replacement = self.root / "replacement"
        replacement.write_text('{"type":"session_meta","payload":{"id":"one"}}\n')
        replacement.replace(self.path)
        self.assertIsNone(self.read()["session_tokens"])
        self.usage(200, 200)
        self.assertEqual(self.read()["session_tokens"], 200)
        self.path.write_text("")
        self.assertIsNone(self.read()["session_tokens"])
        self.path.unlink()
        self.assertIsNone(self.read()["session_tokens"])

    def test_discovery_and_hook_path_preserved_through_notify(self):
        self.event("task_started", turn_id="first")
        self.usage(1200, 1200)
        self.event("task_complete", turn_id="first")
        settings = {"CODEX_SESSIONS_DIR": str(self.root), "CODEX_EVENT_FILE": str(self.root / "event.json")}
        self.assertEqual(CodexUsageCache().read("one", None, settings)["session_tokens"], 1200)
        event_file = self.root / "event.json"
        write_codex_event({"hook_event_name": "UserPromptSubmit", "session_id": "one", "turn_id": "first",
                           "transcript_path": str(self.path), "cwd": "/work/project"}, event_file)
        write_codex_event({"type": "agent-turn-complete", "thread-id": "one", "turn-id": "first"}, event_file)
        self.assertEqual(json.loads(event_file.read_text())["transcript_path"], str(self.path))
        with patch.dict(os.environ, {}, clear=True):
            snapshot = codex_snapshot(settings)
        self.assertEqual(snapshot["session_tokens"], 1200)
        self.assertEqual(snapshot["last_task_tokens"], 1200)
        self.assertEqual(snapshot["state"], "done")

    def test_selection_matches_working_icon_and_optional_pin(self):
        event_file = self.root / "event.json"
        settings = {"CODEX_EVENT_FILE": str(event_file), "CODEX_SESSIONS_DIR": str(self.root)}
        write_codex_event({"hook_event_name": "UserPromptSubmit", "session_id": "one", "turn_id": "first"}, event_file)
        write_codex_event({"type": "agent-turn-complete", "thread-id": "two", "turn-id": "second"}, event_file)
        with patch.dict(os.environ, {}, clear=True):
            snapshot = codex_snapshot(settings)
            self.assertEqual((snapshot["session_id"], snapshot["state"]), ("one", "working"))
            snapshot = codex_snapshot(dict(settings, CODEX_SESSION_ID="two"))
            self.assertEqual((snapshot["session_id"], snapshot["state"]), ("two", "done"))
            self.assertIsNone(codex_snapshot(dict(settings, CODEX_SESSION_ID="missing"))["session_tokens"])

    def test_token_panel_and_compact_values(self):
        self.assertEqual([format_tokens(value) for value in (None, 0, 999, 1200, 1234567)],
                         ["—", "0", "999", "1.2K", "1.23M"])
        fonts = Path(__file__).resolve().parents[1] / "src/turzx_dashboard/assets/fonts"
        theme = {"SHOW_TOKENS": True}
        blank = render_codex_icon("working", theme, fonts)
        counted = render_codex_icon("working", theme, fonts,
                                    {"session_tokens": 1234567, "last_task_tokens": 12345})
        self.assertEqual(blank.crop((0, 0, 275, 325)).tobytes(), counted.crop((0, 0, 275, 325)).tobytes())
        self.assertNotEqual(blank.crop((300, 0, 635, 325)).tobytes(), counted.crop((300, 0, 635, 325)).tobytes())

    def priced_usage(self, input_tokens, cached, output, last=None):
        usage = {"input_tokens": input_tokens, "cached_input_tokens": cached,
                 "output_tokens": output, "total_tokens": input_tokens + output,
                 "reasoning_output_tokens": output // 2}
        self.event("token_count", info={"total_token_usage": usage,
                                       "last_token_usage": usage if last is None else last})

    def cost_settings(self):
        return {"CODEX_MODEL_RATES": {"model-a": {"INPUT": 2, "CACHED_INPUT": 0.2, "OUTPUT": 10},
                                      "model-b": {"INPUT": 4, "CACHED_INPUT": 0.4, "OUTPUT": 20}}}

    def model(self, name):
        self.append({"type": "turn_context", "payload": {"model": name}})

    def test_cost_components_models_duplicates_late_flush_and_repricing(self):
        settings = self.cost_settings()
        self.event("task_started", turn_id="first")
        self.model("model-a")
        self.priced_usage(1000, 600, 100)
        self.priced_usage(1000, 600, 100)  # Duplicate usage is free.
        self.event("task_complete", turn_id="first")
        snapshot = self.reader.read(self.path, "one", settings)
        self.assertAlmostEqual(snapshot["session_cost"], 0.00192)
        self.assertAlmostEqual(snapshot["last_task_cost"], 0.00192)
        self.event("task_started", turn_id="second")
        self.model("model-b")
        self.priced_usage(2000, 1000, 200)
        snapshot = self.reader.read(self.path, "one", settings)
        self.assertAlmostEqual(snapshot["session_cost"], 0.00648)
        self.assertAlmostEqual(snapshot["last_task_cost"], 0.00192)
        self.event("task_complete", turn_id="second")
        self.priced_usage(2100, 1050, 210)  # Completion may precede final usage.
        snapshot = self.reader.read(self.path, "one", settings)
        self.assertAlmostEqual(snapshot["last_task_cost"], 0.00498)
        self.assertAlmostEqual(snapshot["session_cost"], 0.0069)
        self.assertEqual(snapshot, TranscriptUsage().read(self.path, "one", settings))
        settings["CODEX_MODEL_RATES"]["model-b"]["OUTPUT"] = 0
        self.assertAlmostEqual(self.reader.read(self.path, "one", settings)["last_task_cost"], 0.00278)

    def test_fork_inherits_usage_only_up_to_recorded_history_boundary(self):
        settings = self.cost_settings()
        settings["CODEX_SESSIONS_DIR"] = str(self.root)
        self.event("task_started", turn_id="parent")
        self.model("model-a")
        self.priced_usage(1000, 600, 100)
        self.event("task_complete", turn_id="parent")
        boundary = self.path.stat().st_size
        self.event("task_started", turn_id="after-fork")
        self.priced_usage(3000, 1800, 300)
        self.event("task_complete", turn_id="after-fork")

        fork = self.root / "rollout-date-fork.jsonl"
        records = [
            {"type": "session_meta", "payload": {"id": "fork", "history_base": {
                "thread_id": "one", "end_byte_offset": boundary}}},
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "child"}},
            {"type": "turn_context", "payload": {"model": "model-b"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 2000, "cached_input_tokens": 1200,
                                      "output_tokens": 200, "total_tokens": 2200},
                "last_token_usage": {"input_tokens": 1000, "cached_input_tokens": 600,
                                     "output_tokens": 100, "total_tokens": 1100}}}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "child"}},
        ]
        fork.write_text("".join(json.dumps(record) + "\n" for record in records))
        cache = CodexUsageCache()
        snapshot = cache.read("fork", str(fork), settings)
        self.assertEqual(snapshot["session_tokens"], 1000)
        self.assertEqual(snapshot["last_task_tokens"], 500)
        self.assertAlmostEqual(snapshot["session_cost"], 0.00576)
        self.assertAlmostEqual(snapshot["last_task_cost"], 0.00384)
        self.assertEqual(snapshot, cache.read("fork", str(fork), settings))
        self.assertEqual(snapshot, CodexUsageCache().read("fork", str(fork), settings))

        for history in (
                {"thread_id": "missing", "end_byte_offset": boundary},
                {"thread_id": "one", "end_byte_offset": boundary - 1},
                {"thread_id": "one", "end_byte_offset": self.path.stat().st_size + 1},
                {"thread_id": "one", "end_byte_offset": True},
                {"thread_id": "../one", "end_byte_offset": boundary},
                {"thread_id": "fork", "end_byte_offset": boundary}):
            with self.subTest(history=history):
                records[0]["payload"]["history_base"] = history
                fork.write_text("".join(json.dumps(record) + "\n" for record in records))
                unavailable = CodexUsageCache().read("fork", str(fork), settings)
                self.assertIsNone(unavailable["session_cost"])
                self.assertIsNone(unavailable["last_task_cost"])

    def test_nested_fork_preserves_models_and_does_not_inherit_last_task(self):
        settings = self.cost_settings()
        settings["CODEX_SESSIONS_DIR"] = str(self.root)
        self.event("task_started", turn_id="parent")
        self.model("model-a")
        self.priced_usage(1000, 600, 100)
        self.event("task_complete", turn_id="parent")
        parent_id = "one"
        parent_path = self.path
        for session in ("child", "grandchild"):
            path = self.root / f"rollout-date-{session}.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": {
                "id": session, "history_base": {"thread_id": parent_id,
                                                 "end_byte_offset": parent_path.stat().st_size}}}) + "\n")
            snapshot = CodexUsageCache().read(session, str(path), settings)
            self.assertAlmostEqual(snapshot["session_cost"], 0.00192)
            self.assertEqual(snapshot["session_tokens"], 500)
            self.assertIsNone(snapshot["last_task_tokens"])
            self.assertIsNone(snapshot["last_task_cost"])
            parent_id, parent_path = session, path

    def test_cost_unknown_rates_and_components_do_not_look_free(self):
        self.event("task_started", turn_id="first")
        self.model("model-a")
        self.priced_usage(1000, 600, 100)
        self.event("task_complete", turn_id="first")
        for rates in ({}, {"INPUT": 2, "CACHED_INPUT": 0.2},
                      {"INPUT": -1, "CACHED_INPUT": 0, "OUTPUT": 0},
                      {"INPUT": float("nan"), "CACHED_INPUT": 0, "OUTPUT": 0},
                      {"INPUT": True, "CACHED_INPUT": 0, "OUTPUT": 0}):
            settings = {"CODEX_MODEL_RATES": {"model-a": rates}}
            self.assertIsNone(self.reader.read(self.path, "one", settings)["session_cost"])
        settings = {"CODEX_MODEL_RATES": {"model-a": dict.fromkeys(("INPUT", "CACHED_INPUT", "OUTPUT"), 0)}}
        self.assertEqual(self.reader.read(self.path, "one", settings)["session_cost"], 0)
        self.model("unpriced-model")
        self.event("task_started", turn_id="second")
        self.priced_usage(2000, 1000, 200)
        self.event("task_complete", turn_id="second")
        self.assertIsNone(self.reader.read(self.path, "one", settings)["last_task_cost"])
        self.event("task_started", turn_id="priced-again")
        self.model("model-a")
        self.priced_usage(3000, 1600, 300)
        self.event("task_complete", turn_id="priced-again")
        self.assertEqual(self.reader.read(self.path, "one", settings)["last_task_cost"], 0)
        self.assertIsNone(self.reader.read(self.path, "one", settings)["session_cost"])
        self.usage(2500, 300)  # Legacy records without a component breakdown.
        self.assertIsNone(self.reader.read(self.path, "one", settings)["session_cost"])

    def test_cost_abort_reset_and_recovery(self):
        settings = self.cost_settings()
        self.event("task_started", turn_id="first")
        self.model("model-a")
        self.priced_usage(1000, 600, 100)
        self.event("task_complete", turn_id="first")
        self.event("task_started", turn_id="aborted")
        self.priced_usage(2000, 1200, 200)
        self.event("turn_aborted", turn_id="aborted")
        snapshot = self.reader.read(self.path, "one", settings)
        self.assertAlmostEqual(snapshot["session_cost"], 0.00384)
        self.assertAlmostEqual(snapshot["last_task_cost"], 0.00192)
        self.event("task_started", turn_id="reset")
        self.priced_usage(1000, 600, 100)
        self.event("task_complete", turn_id="reset")
        snapshot = self.reader.read(self.path, "one", settings)
        self.assertIsNone(snapshot["session_cost"])
        self.assertIsNone(snapshot["last_task_cost"])
        self.event("task_started", turn_id="recovered")
        self.priced_usage(2000, 1200, 200)
        self.event("task_complete", turn_id="recovered")
        snapshot = self.reader.read(self.path, "one", settings)
        self.assertIsNone(snapshot["session_cost"])
        self.assertAlmostEqual(snapshot["last_task_cost"], 0.00192)

    def test_cost_resumed_history_and_missing_usage_recovery(self):
        settings = self.cost_settings()
        self.event("task_started", turn_id="resumed")
        self.model("model-a")
        self.priced_usage(1000, 600, 100, last={"input_tokens": 500,
                                              "cached_input_tokens": 300, "output_tokens": 50})
        self.event("task_complete", turn_id="resumed")
        snapshot = self.reader.read(self.path, "one", settings)
        self.assertIsNone(snapshot["session_cost"])
        self.assertIsNone(snapshot["last_task_cost"])
        self.event("task_started", turn_id="next")
        self.priced_usage(2000, 1200, 200)
        self.event("task_complete", turn_id="next")
        self.assertAlmostEqual(self.reader.read(self.path, "one", settings)["last_task_cost"], 0.00192)
        self.usage(3300, 1100)  # Missing components invalidate the late completion.
        self.assertIsNone(self.reader.read(self.path, "one", settings)["last_task_cost"])
        self.priced_usage(3000, 1800, 300)
        self.event("task_started", turn_id="recovered")
        self.priced_usage(4000, 2400, 400)
        self.event("task_complete", turn_id="recovered")
        self.assertAlmostEqual(self.reader.read(self.path, "one", settings)["last_task_cost"], 0.00192)

    def test_cost_display(self):
        self.assertEqual(format_cost(0.123456, "CNY"), "¥0.1235")
        self.assertEqual([format_cost(value) for value in (None, 0, 0.123456, float("inf"), -1)],
                         ["—", "$0.0000", "$0.1235", "—", "—"])
        fonts = Path(__file__).resolve().parents[1] / "src/turzx_dashboard/assets/fonts"
        theme = {"SHOW_TOKENS": True, "SHOW_COSTS": True, "TOKEN_Y": 12,
                 "TOKEN_ROW_HEIGHT": 69, "TOKEN_VALUE_SIZE": 28}
        blank = render_codex_icon("working", theme, fonts)
        priced = render_codex_icon("working", theme, fonts,
                                   {"session_cost": 0.1234, "last_task_cost": 0.0123})
        self.assertNotEqual(blank.crop((300, 180, 635, 290)).tobytes(),
                            priced.crop((300, 180, 635, 290)).tobytes())

    def test_cny_four_rates_and_model_override(self):
        settings = {"CODEX_COST_CURRENCY": "CNY", "CODEX_MODEL_RATES": {
            "*": {"INPUT": 0.8, "OUTPUT": 4, "CACHE_READ": 0.04, "CACHE_WRITE": 1},
            "model-b": {"INPUT": 1.6, "OUTPUT": 8, "CACHE_READ": 0.08, "CACHE_WRITE": 2}}}
        self.event("task_started", turn_id="first")
        self.model("model-a")
        usage = {"input_tokens": 1_000_000, "cached_input_tokens": 600_000,
                 "cache_write_input_tokens": 100_000, "output_tokens": 100_000,
                 "total_tokens": 1_100_000}
        self.event("token_count", info={"total_token_usage": usage, "last_token_usage": usage})
        self.event("task_complete", turn_id="first")
        snapshot = self.reader.read(self.path, "one", settings)
        # 300K regular input, 600K reads, 100K writes, and 100K output.
        self.assertAlmostEqual(snapshot["session_cost"], 0.764)
        self.assertAlmostEqual(snapshot["last_task_cost"], 0.764)
        self.assertEqual(snapshot["cost_currency"], "CNY")
        self.event("task_started", turn_id="second")
        self.model("model-b")
        self.event("token_count", info={"total_token_usage": {key: value * 2 for key, value in usage.items()},
                                       "last_token_usage": usage})
        self.event("task_complete", turn_id="second")
        snapshot = self.reader.read(self.path, "one", settings)
        self.assertAlmostEqual(snapshot["last_task_cost"], 1.528)
        self.assertAlmostEqual(snapshot["session_cost"], 2.292)
        self.assertEqual(snapshot, TranscriptUsage().read(self.path, "one", settings))

    def test_invalid_cache_write_counts_are_unavailable(self):
        for writes in (-1, True, "100", 500):
            reader = TranscriptUsage()
            self.event("task_started", turn_id=str(writes))
            self.model("model-a")
            usage = {"input_tokens": 1000, "cached_input_tokens": 600,
                     "cache_write_input_tokens": writes, "output_tokens": 100,
                     "total_tokens": 1100}
            self.event("token_count", info={"total_token_usage": usage, "last_token_usage": usage})
            self.event("task_complete", turn_id=str(writes))
            snapshot = reader.read(self.path, "one", self.cost_settings())
            self.assertIsNone(snapshot["session_cost"])
            self.assertIsNone(snapshot["last_task_cost"])


if __name__ == "__main__":
    unittest.main()
