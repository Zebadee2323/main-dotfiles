import asyncio
import io
import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

import agent_comms as app
import pane_identity
from test_agent_comms import Fixture


class AudioControlTests(Fixture):
    def audio(self, action, **kwargs):
        _, stream = self.request("audio", action=action, **kwargs)
        result = json.loads(stream.readline())
        self.assertTrue(result["ok"], result)
        return result["audio"]

    def test_toggle_indefinite_mute_and_status_without_config(self):
        self.start_server()
        self.assertFalse(self.audio("status")["muted"])
        self.assertEqual(self.audio("toggle"), {"muted": True, "remaining_seconds": None})
        self.assertFalse(self.audio("toggle")["muted"])
        self.assertTrue(self.audio("mute")["muted"])
        self.assertFalse(self.audio("unmute")["muted"])
        self.paths.config.unlink()
        result = self.cli("audio", "toggle")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["muted"])

    def test_timer_notifies_all_watchers_and_resumes_automatically(self):
        self.start_server()
        watchers = [self.request("watch")[1] for _ in range(2)]
        for stream in watchers:
            self.assertFalse(json.loads(stream.readline())["audio"]["muted"])
        self.audio("mute", seconds=0.1)
        for stream in watchers:
            self.assertTrue(json.loads(stream.readline())["audio"]["muted"])
            self.assertFalse(json.loads(stream.readline())["audio"]["muted"])

    def test_unmute_cancels_old_timer_and_muting_releases_a_speech_lease(self):
        self.start_server()
        _, watcher = self.request("watch")
        watcher.readline()
        self.transport_send("a", "hello")
        preparing = json.loads(watcher.readline())["message"]
        _, claim = self.request("claim-speech", uid=preparing["uid"])
        self.assertTrue(json.loads(claim.readline())["claimed"])
        self.audio("mute", seconds=0.1)
        self.audio("unmute")
        self.audio("mute")
        while True:
            update = json.loads(watcher.readline())
            if update["message"] and update["message"]["phase"] == "displaying":
                break
        self.assertNotIn("voice", update["message"])
        self.assertTrue(self.audio("status")["muted"])

    def test_invalid_durations_and_actions_do_not_change_state(self):
        self.start_server()
        for action, seconds in (("mute", 0), ("mute", -1), ("mute", True), ("mute", "60"),
                                ("mute", float("nan")), ("mute", 86401), ("toggle", 1), ("bad", None)):
            _, stream = self.request("audio", action=action, seconds=seconds)
            self.assertFalse(json.loads(stream.readline())["ok"])
        self.assertFalse(self.audio("status")["muted"])
        for value in ("0", "-1", "nan", "inf", "86401"):
            self.assertEqual(self.cli("audio", "mute", "--seconds", value).returncode, 1)
        self.assertEqual(self.cli("audio", "toggle", "--seconds", "1").returncode, 1)


class PresentationMuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_mute_cancels_current_speech_without_replaying(self):
        started, stopped = asyncio.Event(), asyncio.Event()

        async def voice(*args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        state = {"message": {"id": "a", "name": "Alice", "msg": "Hello", "phase": "displaying",
                             "started_at": 0, "duration": 1}, "arrived": 0}
        emit = Mock()
        with patch.object(app, "watcher_speech", side_effect=voice) as speech:
            task = asyncio.create_task(app.present_message(None, state, {}, emit, 80, False, False))
            try:
                await asyncio.wait_for(started.wait(), 1)
                state["audio_muted"] = True
                await asyncio.wait_for(stopped.wait(), 1)
                state["audio_muted"] = False
                await asyncio.sleep(0.1)
                self.assertEqual(speech.call_count, 1)
                self.assertGreater(emit.call_count, 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class PaneTitleTests(Fixture):
    pane_env = {"TUIOS_ENV": "1", "TUIOS_PANE_ID": "own", "TUIOS_SESSION": "work"}

    def test_only_exact_owned_prefix_is_replaced_and_suffix_preserved(self):
        self.assertEqual(pane_identity.prefixed_title("OC | Fix bug", "[Alice] "), "[Alice] OC | Fix bug")
        self.assertEqual(pane_identity.prefixed_title("[Alice] OC | New task", "[Bob] ", "[Alice] "),
                         "[Bob] OC | New task")
        self.assertEqual(pane_identity.prefixed_title("[Alice] OC", "[Alice] ", "[Alice] "), "[Alice] OC")
        self.assertEqual(pane_identity.prefixed_title("[Human] My title", "[Bob] ", "[Alice] "),
                         "[Bob] [Human] My title")

    def test_idempotent_rename_and_identity_change_use_saved_prefix(self):
        title = "OC | Original title"

        def command(args, **kwargs):
            nonlocal title
            if args[1] == "get-window":
                return subprocess.CompletedProcess(args, 0, json.dumps({"success": True, "id": "own", "display_name": title}))
            title = args[-1]
            return subprocess.CompletedProcess(args, 0, "")

        run = Mock(side_effect=command)
        with patch.dict(os.environ, self.pane_env), patch.object(pane_identity, "inventory", return_value={"own"}):
            app.pane_binding(self.paths, app.load_config(self.paths.config))
            for name in ("Alice", "Alice", "Bob"):
                pane_identity.update_title(self.paths, name, {"pane_title": title}, private_dir=app.private_dir,
                                           lock=app.FileLock, atomic_json=app.atomic_json, run=run)
            self.assertEqual(title, "[Bob] OC | Original title")
            self.assertEqual(sum(c.args[0][1] == "set-window" for c in run.call_args_list), 2)
            title = "User renamed this"
            pane_identity.update_title(self.paths, "Bob", {"pane_title": title}, private_dir=app.private_dir,
                                       lock=app.FileLock, atomic_json=app.atomic_json, run=run)
            self.assertEqual(title, "[Bob] User renamed this")

    def test_naming_failure_does_not_break_context_json_or_retry(self):
        source = {"pane_id": "own", "pane_title": "Task", "introduce": False}
        with patch.dict(os.environ, self.pane_env), patch.object(pane_identity, "inventory", return_value={"own"}), \
             patch.object(app, "checked_context", return_value=source), \
             patch.object(pane_identity.subprocess, "run", side_effect=PermissionError("denied")) as run, \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            result = app.identify(self.paths, app.load_config(self.paths.config))
        self.assertEqual(set(result), {"id", "name", "prefix"})
        self.assertIn("pane name unchanged", errors.getvalue())
        self.assertEqual(run.call_count, 1)
