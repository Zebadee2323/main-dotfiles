"""Rejected sends return checked context, never silently rewrite or resend."""
import asyncio
import io
import json
from unittest.mock import AsyncMock, patch

import agent_comms as app
from test_agent_comms import Fixture


class SendContextTests(Fixture):
    def invoke(self, args, *, refresh_error=None, real_binding=False, send_error=None,
               send_result=None):
        output, errors = io.StringIO(), io.StringIO()
        fresh = {"id": "a", "name": "Alice", "prefix": "Alice here, of the work session."}
        source = {"session_name": "work", "introduce": True}
        with patch.object(app, "prepare_output"), patch.object(app.Paths, "resolve", return_value=self.paths), \
             patch.object(app, "checked_context", return_value=source), \
             patch.object(app, "pane_binding", **({"wraps": app.pane_binding} if real_binding else {"return_value": "a"})), \
             patch.object(app, "listener_context", new=AsyncMock(return_value=fresh, side_effect=refresh_error)) as refresh, \
              patch.object(app, "send", new=AsyncMock(side_effect=send_error, return_value=send_result)) as send, \
             patch("sys.stdout", output), patch("sys.stderr", errors):
            code = app.main(["send", *args])
        return code, output.getvalue(), errors.getvalue(), refresh, send, fresh

    def test_identity_and_both_prefix_mismatches_return_fresh_context_without_send(self):
        prefix = "Alice here, of the work session."
        for args in (["--id", "stale", "--msg", "Ready."],
                     ["--id", "a", "--msg", "Ready."],
                     ["--id", "a", "--msg", prefix + " Ready.", "--tts", "Ready."]):
            with self.subTest(args=args):
                code, output, errors, refresh, send, fresh = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                line = next(line for line in errors.splitlines() if line.startswith("agent-comms: Fresh context: "))
                self.assertEqual(json.loads(line.removeprefix("agent-comms: Fresh context: ")), fresh)
                self.assertIn("Message was not queued", errors)
                self.assertIn("recompose both --msg and --tts", errors)
                refresh.assert_awaited_once_with(self.paths)
                send.assert_not_called()

    def test_refresh_failure_preserves_rejection_and_does_not_retry(self):
        for error in (app.CommsError("offline"), PermissionError("denied"), asyncio.TimeoutError()):
            with self.subTest(error=error):
                code, _, errors, refresh, send, _ = self.invoke(
                    ["--id", "stale", "--msg", "Ready."], refresh_error=error)
                self.assertEqual(code, 1)
                self.assertIn("Identity mismatch", errors)
                self.assertIn("Context refresh failed", errors)
                self.assertIn("Run 'agent-comms context'", errors)
                self.assertNotIn("Fresh context:", errors)
                refresh.assert_awaited_once()
                send.assert_not_called()

    def test_missing_or_removed_identity_returns_context_without_sending(self):
        for saved in (None, {"id": "removed", "daemon": "daemon", "pane_id": "pane"}):
            with self.subTest(saved=saved), \
                    patch.object(app.pane_identity, "owner", return_value=("key", "daemon", "pane")):
                app.private_dir(self.paths.state)
                app.atomic_json(self.paths.state / "pane-identities.json", {
                    "version": 1, "bindings": {} if saved is None else {"key": saved}, "scanned": {}})
                code, output, errors, refresh, send, fresh = self.invoke(
                    ["--id", "stale", "--msg", "Old name here. Ready."], real_binding=True)
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn("Pane has no current identity", errors)
                self.assertIn("Message was not queued", errors)
                line = next(line for line in errors.splitlines() if line.startswith("agent-comms: Fresh context: "))
                self.assertEqual(json.loads(line.removeprefix("agent-comms: Fresh context: ")), fresh)
                refresh.assert_awaited_once_with(self.paths)
                send.assert_not_called()

    def test_missing_identity_refresh_failure_does_not_send_or_retry(self):
        for error in (app.CommsError("offline"), PermissionError("denied"), asyncio.TimeoutError()):
            with self.subTest(error=error), \
                    patch.object(app.pane_identity, "owner", return_value=("key", "daemon", "pane")):
                code, _, errors, refresh, send, _ = self.invoke(
                    ["--id", "stale", "--msg", "Ready."], real_binding=True, refresh_error=error)
                self.assertEqual(code, 1)
                self.assertIn("Pane has no current identity", errors)
                self.assertIn("Context refresh failed", errors)
                self.assertNotIn("Fresh context:", errors)
                refresh.assert_awaited_once_with(self.paths)
                send.assert_not_called()
                self.assertFalse(self.paths.state.exists())

    def test_corrupt_binding_does_not_trigger_identity_recovery(self):
        app.private_dir(self.paths.state)
        (self.paths.state / "pane-identities.json").write_text("not json", encoding="utf-8")
        with patch.object(app.pane_identity, "owner", return_value=("key", "daemon", "pane")):
            code, _, errors, refresh, send, _ = self.invoke(
                ["--id", "stale", "--msg", "Ready."], real_binding=True)
        self.assertEqual(code, 1)
        self.assertIn("Corrupt pane identity mapping", errors)
        refresh.assert_not_called()
        send.assert_not_called()

    def test_valid_send_and_unrelated_validation_error_do_not_refresh(self):
        for message, expected in (("Alice here, of the work session. Ready.", 0), ("", 1)):
            code, _, _, refresh, send, _ = self.invoke(["--id", "a", "--msg", message])
            self.assertEqual(code, expected)
            refresh.assert_not_called()
            self.assertEqual(send.await_count, 1 if expected == 0 else 0)

    def test_success_prints_only_listener_total(self):
        prefix = "Alice here, of the work session."
        for tts in (None, prefix + " Hello world.", prefix + " e\u0301 界."):
            with self.subTest(tts=tts):
                message = prefix + " A much longer message for the captions, not the voice."
                args = ["--id", "a", "--msg", message]
                if tts is not None:
                    args.extend(["--tts", tts])
                code, output, errors, refresh, send, _ = self.invoke(
                    args, send_result={"estimated_total_seconds": 12.34})
                self.assertEqual(code, 0)
                self.assertEqual(errors, "")
                self.assertEqual(output, "Estimated total time: 12.3 seconds (approximate).\n")
                send.assert_awaited_once()
                refresh.assert_not_called()

    def test_older_listener_reports_accepted_without_inventing_total(self):
        code, output, _, _, send, _ = self.invoke(
            ["--id", "a", "--msg", "Alice here, of the work session. Ready."],
            send_result={"ok": True})
        self.assertEqual(code, 0)
        self.assertIn("Message queued. Total time unavailable; restart the listener", output)
        send.assert_awaited_once()

    def test_failed_send_does_not_print_estimate(self):
        code, output, errors, refresh, send, _ = self.invoke(
            ["--id", "a", "--msg", "Alice here, of the work session. Ready."],
            send_error=app.CommsError("offline"))
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("offline", errors)
        send.assert_awaited_once()
        refresh.assert_not_called()
