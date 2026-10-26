import asyncio
import io
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
import tuios_context
from test_agent_comms import Fixture


class PrefixTests(Fixture):
    source = {"session_name": "work", "pane_id": "pane", "workspace": 2, "introduce": True}
    prefix = "Alice here, of the work session."

    def config(self):
        return app.load_config(self.paths.config)

    def invoke(self, args, source=None):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(app, "prepare_output"), patch.object(app.Paths, "resolve", return_value=self.paths), \
             patch.object(app, "pane_binding", return_value="a"), \
             patch.object(tuios_context, "context", return_value=self.source if source is None else source), \
             patch("sys.stdout", output), patch("sys.stderr", errors):
            code = app.main(args)
        return code, output.getvalue(), errors.getvalue()

    def test_id_returns_name_and_prefix_without_listener(self):
        code, text, error = self.invoke(["context"])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)["prefix"], self.prefix)
        self.assertEqual(json.loads(text)["name"], "Alice")
        code, text, error = self.invoke(["context"], source={})
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(text)["prefix"], "")
        self.assertFalse(self.paths.runtime.exists())
        self.assertFalse(self.paths.state.exists())

    def test_id_json_contains_only_required_fields(self):
        code, text, error = self.invoke(["context"])
        self.assertEqual(code, 0, error)
        result = json.loads(text)
        self.assertEqual(result, {"name": "Alice", "prefix": self.prefix})

    def test_prefix_command_removed_and_unknown_identity_is_error(self):
        self.assertEqual(self.cli("id").returncode, 2)
        self.assertEqual(self.cli("prefix", "--id", "a").returncode, 2)
        self.assertEqual(self.cli("send", "--id", "a", "--msg", "Ready.").returncode, 2)
        with self.assertRaisesRegex(app.CommsError, "Unknown identity"):
            app.validate_message(self.config(), "missing", "Ready.")

    def test_valid_prefix_is_not_inserted_or_rewritten(self):
        message = self.prefix + " Ready."
        tts = self.prefix + ' <break time="0.3s" /> Ready.'
        result = app.validate_message(self.config(), "a", message, tts, self.source)
        self.assertEqual(result["msg"], message)
        self.assertEqual(result["tts"], tts)
        self.assertEqual(result["session_name"], "work")

    def test_missing_wrong_stale_and_duplicated_prefixes_are_errors_in_each_field(self):
        good = self.prefix + " Ready."
        for bad in ("Ready.", "Bob here, of the work session. Ready.",
                    "Alice here, of the old session. Ready.", self.prefix + " " + self.prefix + " Ready.",
                    self.prefix + ' <break time="0.3s" /> ' + self.prefix + " Ready.",
                    self.prefix + "No separator", self.prefix,
                    '<break time="0.3s" /> ' + good):
            for field in ("--msg", "--tts"):
                with self.subTest(bad=bad, field=field), self.assertRaises(app.CommsError) as error:
                    app.validate_message(self.config(), "a", bad if field == "--msg" else good,
                                         bad if field == "--tts" else good, self.source)
                self.assertIn(field, str(error.exception))
                self.assertIn("agent-comms context", str(error.exception))
                self.assertIn("Message was not queued", str(error.exception))

    def test_no_required_prefix_rejects_stale_introductions(self):
        for prefix in ("", '<break time="0.3s" /> '):
            with self.assertRaises(app.CommsError) as error:
                app.validate_message(self.config(), "a", prefix + self.prefix + " Ready.", source={})
            self.assertIn("no introduction", str(error.exception))
        result = app.validate_message(self.config(), "a", "Ready.", source={})
        self.assertEqual(result["msg"], "Ready.")

    def test_send_rechecks_context_and_rejects_focus_race_before_network(self):
        with patch.object(app, "send", new=AsyncMock()) as send:
            code, _, error = self.invoke(["send", "--msg", self.prefix + " Ready."],
                                         source={**self.source, "session_name": "changed"})
        self.assertEqual(code, 1)
        self.assertIn("changed session.", error)
        send.assert_not_called()

    def test_checked_send_passes_both_strings_unchanged(self):
        message = self.prefix + " Ready."
        with patch.object(app, "send", new=AsyncMock()) as send:
            code, _, error = self.invoke(["send", "--msg", message, "--tts", message])
        self.assertEqual(code, 0, error)
        send.assert_awaited_once_with(self.paths, "a", message, message, self.source)

    def test_unavailable_tuios_context_is_not_silently_treated_as_empty(self):
        env = {"TUIOS_ENV": "1", "TUIOS_SESSION": "work", "TUIOS_PANE_ID": "pane"}
        run = Mock(side_effect=PermissionError("forbidden"))
        with self.assertRaises(tuios_context.ContextError):
            tuios_context.context("Alice", run=run, env=env, strict=True)
        self.assertEqual(run.call_count, 1)
        with self.assertRaises(tuios_context.ContextError):
            tuios_context.context("Alice", run=run, env={"TUIOS_ENV": "1"}, strict=True)
        with patch.object(app, "prepare_output"), patch.object(app.Paths, "resolve", return_value=self.paths), \
             patch.object(tuios_context, "context", side_effect=tuios_context.ContextError("Cannot check prefix")), \
             patch.object(app, "send", new=AsyncMock()) as send, patch("sys.stderr", io.StringIO()):
            self.assertEqual(app.main(["send", "--msg", "Ready."]), 1)
            send.assert_not_called()

    def test_listener_rejects_missing_prefix_before_history_or_queue(self):
        self.start_server()
        _, stream = self.request("send", id="a", msg="Ready.", source=self.source)
        result = json.loads(stream.readline())
        self.assertFalse(result["ok"])
        self.assertIn("Expected prefix", result["error"])
        self.assertEqual(json.loads(self.cli("history", "--json").stdout), [])
        _, subscriber = self.request("watch")
        self.assertIsNone(json.loads(subscriber.readline())["message"])

    def test_legacy_listener_is_rejected_before_it_can_auto_prefix(self):
        with patch.object(app, "endpoint", return_value={"protocol_version": app.VERSION}), \
             patch.object(app, "rpc", new=AsyncMock()) as rpc:
            with self.assertRaises(app.CommsError) as error:
                asyncio.run(app.send(self.paths, "a", self.prefix + " Ready.", source=self.source))
        self.assertIn("restart", str(error.exception))
        rpc.assert_not_called()
