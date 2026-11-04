"""Rejected sends return checked context, never silently rewrite or resend."""
import asyncio
import io
import json
from unittest.mock import AsyncMock, patch

import agent_comms as app
import pane_identity
from test_agent_comms import Fixture, seed_binding

EPOCH = "e" * 16
PREFIX = "Alice here, of the work session."
FRESH_KEYS = {"id", "name", "epoch", "prefix"}


class SendContextTests(Fixture):
    def invoke(self, args, *, refresh_error=None, real_binding=False, send_error=None,
               send_result=None, binding=(EPOCH, "Alice")):
        output, errors = io.StringIO(), io.StringIO()
        fresh = {"id": "Alice", "name": "Alice", "epoch": EPOCH, "prefix": PREFIX}
        source = {"session_name": "work", "introduce": True}
        caller = {"wraps": app.caller_binding} if real_binding else {"return_value": binding}
        with patch.object(app, "prepare_output"), patch.object(app.Paths, "resolve", return_value=self.paths), \
             patch.object(app, "checked_context", return_value=source), \
             patch.object(app, "caller_binding", **caller), \
             patch.dict(app.os.environ, {"TUIOS_ENV": "1"}), \
             patch.object(app, "listener_context", new=AsyncMock(return_value=fresh, side_effect=refresh_error)) as refresh, \
             patch.object(app, "send", new=AsyncMock(side_effect=send_error, return_value=send_result)) as send, \
             patch("sys.stdout", output), patch("sys.stderr", errors):
            code = app.main(["send", *args])
        return code, output.getvalue(), errors.getvalue(), refresh, send, fresh

    def fresh_context(self, errors):
        line = next(line for line in errors.splitlines() if line.startswith("agent-comms: Fresh context: "))
        return json.loads(line.removeprefix("agent-comms: Fresh context: "))

    def test_name_epoch_and_both_prefix_mismatches_return_fresh_context_without_send(self):
        for args, reason in (
            (["--name", "Stale", "--epoch", EPOCH, "--msg", PREFIX + " Ready."], "Name mismatch"),
            (["--name", "Alice", "--msg", PREFIX + " Ready."], "Missing --epoch"),
            (["--name", "Alice", "--epoch", "0" * 16, "--msg", PREFIX + " Ready."], "Epoch mismatch"),
            (["--name", "Alice", "--epoch", EPOCH, "--msg", "Ready."], "Prefix mismatch in --msg"),
            (["--name", "Alice", "--epoch", EPOCH, "--msg", PREFIX + " Ready.", "--tts", "Ready."],
             "Prefix mismatch in --tts"),
        ):
            with self.subTest(args=args):
                code, output, errors, refresh, send, fresh = self.invoke(args)
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn(reason, errors)
                context = self.fresh_context(errors)
                self.assertEqual(context, fresh)
                self.assertEqual(set(context), FRESH_KEYS)
                self.assertIn("Message was not queued", errors)
                self.assertIn("recompose both --msg and --tts", errors)
                self.assertIn("--name NAME --epoch EPOCH", errors)
                refresh.assert_awaited_once_with(self.paths)
                send.assert_not_called()

    def test_refresh_failure_preserves_rejection_and_does_not_retry(self):
        for args, reason in ((["--name", "Stale", "--epoch", EPOCH, "--msg", "Ready."], "Name mismatch"),
                             (["--name", "Alice", "--msg", "Ready."], "Missing --epoch"),
                             (["--name", "Alice", "--epoch", "0" * 16, "--msg", "Ready."], "Epoch mismatch")):
            for error in (app.CommsError("offline"), PermissionError("denied"), asyncio.TimeoutError()):
                with self.subTest(args=args, error=error):
                    code, _, errors, refresh, send, _ = self.invoke(args, refresh_error=error)
                    self.assertEqual(code, 1)
                    self.assertIn(reason, errors)
                    self.assertIn("Message was not queued", errors)
                    self.assertIn("Context refresh failed", errors)
                    self.assertIn("Run 'agent-comms context'", errors)
                    self.assertNotIn("Fresh context:", errors)
                    refresh.assert_awaited_once()
                    send.assert_not_called()

    def test_missing_or_removed_binding_returns_context_without_sending(self):
        other = self.pane_env("someone-else")
        for removed in (False, True):
            if removed:
                seed_binding(self.paths, other, "Bob")  # State exists, but not for this owner.
            with self.subTest(removed=removed), \
                    patch.object(pane_identity, "owner", return_value=("key", "daemon", "pane")):
                code, output, errors, refresh, send, fresh = self.invoke(
                    ["--name", "Alice", "--epoch", EPOCH, "--msg", "Old name here. Ready."], real_binding=True)
                self.assertEqual(code, 1)
                self.assertEqual(output, "")
                self.assertIn("Caller has no current agent name", errors)
                self.assertIn("Message was not queued", errors)
                self.assertEqual(self.fresh_context(errors), fresh)
                refresh.assert_awaited_once_with(self.paths)
                send.assert_not_called()

    def test_missing_binding_refresh_failure_does_not_send_or_retry(self):
        for error in (app.CommsError("offline"), PermissionError("denied"), asyncio.TimeoutError()):
            with self.subTest(error=error), \
                    patch.object(pane_identity, "owner", return_value=("key", "daemon", "pane")):
                code, _, errors, refresh, send, _ = self.invoke(
                    ["--name", "Alice", "--epoch", EPOCH, "--msg", "Ready."], real_binding=True, refresh_error=error)
                self.assertEqual(code, 1)
                self.assertIn("Caller has no current agent name", errors)
                self.assertIn("Context refresh failed", errors)
                self.assertNotIn("Fresh context:", errors)
                refresh.assert_awaited_once_with(self.paths)
                send.assert_not_called()
                # The send path is read-only: it never creates state, locks, or bindings.
                self.assertFalse(self.paths.state.exists())

    def test_corrupt_state_does_not_trigger_context_recovery(self):
        app.private_dir(self.paths.state)
        (self.paths.state / "allocation.json").write_text("not json", encoding="utf-8")
        with patch.object(pane_identity, "owner", return_value=("key", "daemon", "pane")):
            code, _, errors, refresh, send, _ = self.invoke(
                ["--name", "Alice", "--epoch", EPOCH, "--msg", "Ready."], real_binding=True)
        self.assertEqual(code, 1)
        self.assertIn("Corrupt allocation state", errors)
        self.assertNotIn("Fresh context:", errors)
        refresh.assert_not_called()
        send.assert_not_called()

    def test_stale_epoch_after_reset_is_rejected_even_when_name_matches(self):
        env = self.pane_env("reset-pane")
        old = seed_binding(self.paths, env, "Alice")
        config = app.load_config(self.paths.config)
        pane_identity.store(self.paths, lock=app.FileLock, atomic_json=app.atomic_json,
                            private_dir=app.private_dir).reset(config)
        new = seed_binding(self.paths, env, "Alice")  # Same owner, same repeated name.
        self.assertNotEqual(old, new)
        with patch.object(pane_identity, "owner", return_value=pane_identity.owner(env)):
            self.assertEqual(app.caller_binding(self.paths), (new, "Alice"))
            code, output, errors, refresh, send, _ = self.invoke(
                ["--name", "Alice", "--epoch", old, "--msg", PREFIX + " Ready."], real_binding=True)
            self.assertEqual(code, 1)
            self.assertEqual(output, "")
            self.assertIn("Epoch mismatch", errors)
            refresh.assert_awaited_once_with(self.paths)
            send.assert_not_called()
            code, _, errors, _, send, _ = self.invoke(
                ["--name", "Alice", "--epoch", new, "--msg", PREFIX + " Ready."], real_binding=True,
                send_result={"estimated_total_seconds": 1})
            self.assertEqual(code, 0, errors)
            send.assert_awaited_once()

    def test_listener_rejects_stale_epoch_after_reset_even_when_name_matches(self):
        self.start_server()
        env = self.pane_env("reset-pane")
        old = seed_binding(self.paths, env, "Alice")
        pane_identity.store(self.paths, lock=app.FileLock, atomic_json=app.atomic_json,
                            private_dir=app.private_dir).reset(app.load_config(self.paths.config))
        new = seed_binding(self.paths, env, "Alice")
        _, stream = self.request("send", name="Alice", epoch=old, pane_env=env, msg="Ready.")
        reply = json.loads(stream.readline())
        self.assertFalse(reply["ok"])
        self.assertTrue(reply.get("context_mismatch"))
        self.assertIn("mismatch", reply["error"])
        self.assertEqual(json.loads(self.cli("history", "--json").stdout), [])
        _, stream = self.request("send", name="Alice", epoch=new, pane_env=env, msg="Ready.")
        self.assertTrue(json.loads(stream.readline())["ok"])

    def test_name_and_id_together_are_rejected_by_argparse(self):
        result = self.cli("send", "--name", "Alice", "--id", "Alice", "--epoch", EPOCH, "--msg", "Ready.")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with argument", result.stderr)

    def test_deprecated_id_alias_sends_with_a_note(self):
        code, output, errors, refresh, send, _ = self.invoke(
            ["--id", "Alice", "--epoch", EPOCH, "--msg", PREFIX + " Ready."],
            send_result={"estimated_total_seconds": 1})
        self.assertEqual(code, 0, errors)
        self.assertIn("--id is deprecated", errors)
        send.assert_awaited_once()
        self.assertEqual(send.await_args.args[1:3], ("Alice", EPOCH))
        refresh.assert_not_called()

    def test_valid_send_and_unrelated_validation_error_do_not_refresh(self):
        for message, expected in ((PREFIX + " Ready.", 0), ("", 1)):
            with self.subTest(message=message):
                code, _, _, refresh, send, _ = self.invoke(["--name", "Alice", "--epoch", EPOCH, "--msg", message])
                self.assertEqual(code, expected)
                refresh.assert_not_called()
                self.assertEqual(send.await_count, 1 if expected == 0 else 0)

    def test_success_prints_listener_total_then_message_id(self):
        message_id = "0123456789abcdef" * 2
        for tts in (None, PREFIX + " Hello world.", PREFIX + " e\u0301 界."):
            with self.subTest(tts=tts):
                message = PREFIX + " A much longer message for the captions, not the voice."
                args = ["--name", "Alice", "--epoch", EPOCH, "--msg", message]
                if tts is not None:
                    args.extend(["--tts", tts])
                code, output, errors, refresh, send, _ = self.invoke(
                    args, send_result={"estimated_total_seconds": 12.34, "message_id": message_id, "warnings": []})
                self.assertEqual(code, 0)
                self.assertEqual(errors, "")
                self.assertEqual(output, "Estimated total time: 12.3 seconds (approximate).\n"
                                         f"Message ID: {message_id}\n")
                send.assert_awaited_once()
                refresh.assert_not_called()

    def test_listener_warnings_go_to_stderr(self):
        code, output, errors, _, _, _ = self.invoke(
            ["--name", "Alice", "--epoch", EPOCH, "--msg", PREFIX + " Ready."],
            send_result={"estimated_total_seconds": 1, "message_id": "a" * 32, "warnings": ["Unknown recipient"]})
        self.assertEqual(code, 0)
        self.assertIn("agent-comms: warning: Unknown recipient", errors)
        self.assertNotIn("warning", output)

    def test_older_listener_reports_accepted_without_inventing_total(self):
        code, output, _, _, send, _ = self.invoke(
            ["--name", "Alice", "--epoch", EPOCH, "--msg", PREFIX + " Ready."],
            send_result={"ok": True})
        self.assertEqual(code, 0)
        self.assertIn("Message queued. Total time unavailable; restart the listener", output)
        self.assertNotIn("Message ID", output)
        send.assert_awaited_once()

    def test_failed_send_does_not_print_estimate(self):
        code, output, errors, refresh, send, _ = self.invoke(
            ["--name", "Alice", "--epoch", EPOCH, "--msg", PREFIX + " Ready."],
            send_error=app.CommsError("offline"))
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("offline", errors)
        send.assert_awaited_once()
        refresh.assert_not_called()

    def test_listener_context_mismatch_on_send_returns_fresh_context_without_retry(self):
        code, output, errors, refresh, send, fresh = self.invoke(
            ["--name", "Alice", "--epoch", EPOCH, "--msg", PREFIX + " Ready."],
            send_error=app.ContextMismatch("Name/epoch mismatch. Message was not queued."))
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertEqual(self.fresh_context(errors), fresh)
        send.assert_awaited_once()
        refresh.assert_awaited_once_with(self.paths)
