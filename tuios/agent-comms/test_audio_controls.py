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
        self.assertEqual(self.audio("toggle"), {"muted": True, "remaining_seconds": None,
                                                "volume": 100, "volume_notice_until": 0})
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

    def test_volume_cycles_for_all_watchers_and_preserves_timed_mute(self):
        self.start_server()
        watchers = [self.request("watch")[1] for _ in range(2)]
        for stream in watchers:
            self.assertEqual(json.loads(stream.readline())["audio"]["volume"], 100)
        self.audio("mute", seconds=0.2)
        for stream in watchers:
            stream.readline()
        for expected in (10, 25, 50, 100, 10):
            status = self.audio("cycle-volume")
            self.assertEqual(status["volume"], expected)
            self.assertTrue(status["muted"])
            for stream in watchers:
                self.assertEqual(json.loads(stream.readline())["audio"], status)
        for stream in watchers:
            resumed = json.loads(stream.readline())["audio"]
            self.assertFalse(resumed["muted"])
            self.assertEqual(resumed["volume"], 10)

    def test_dock_click_routes_mouse_buttons(self):
        self.start_server()
        cases = (("3", False, 100, False), ("2", False, 100, False),
                 ("1", True, 10, False), ("1", False, 10, True),
                 ("1", True, 25, True), ("3", True, 25, True))
        for button, ctrl, volume, muted in cases:
            with patch.dict(os.environ, {**self.env, "TUIOS_CLICK_BUTTON": button}), \
                 patch.object(app, "control_pressed", return_value=ctrl), \
                 patch("sys.stdout", new_callable=io.StringIO):
                result = app.main(["audio", "dock-click"])
            self.assertEqual(result, 0)
            status = self.audio("status")
            self.assertEqual(status["volume"], volume)
            self.assertEqual(status["muted"], muted)

    def test_macos_control_modifier_mask(self):
        quartz = Mock()
        with patch.object(app.sys, "platform", "darwin"), \
             patch("ctypes.CDLL", return_value=quartz):
            for flags, expected in ((0, False), (1 << 17, False), (1 << 18, True),
                                    ((1 << 18) | (1 << 17), True)):
                quartz.CGEventSourceFlagsState.return_value = flags
                self.assertEqual(app.control_pressed(), expected)
                quartz.CGEventSourceFlagsState.assert_called_with(0)

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

    def test_known_agent_prefixes_are_normalized_and_other_labels_preserved(self):
        self.assertEqual(pane_identity.prefixed_title("OC | Fix bug", "[Alice] "), "[Alice] OC | Fix bug")
        self.assertEqual(pane_identity.prefixed_title("[Alice] OC | New task", "[Bob] ", "[Alice] "),
                         "[Bob] OC | New task")
        self.assertEqual(pane_identity.prefixed_title("[Alice] OC", "[Alice] ", "[Alice] "), "[Alice] OC")
        self.assertEqual(pane_identity.prefixed_title("[Human] My title", "[Bob] ", "[Alice] "),
                         "[Bob] [Human] My title")
        self.assertEqual(pane_identity.prefixed_title("[Alice] [Bob] OC | Task", "[Bob] ", names=["Alice", "Bob"]),
                         "[Bob] OC | Task")
        self.assertEqual(pane_identity.prefixed_title("[Work] OC | Task", "[Bob] ", names=["Alice", "Bob"]),
                         "[Bob] [Work] OC | Task")

    def test_idempotent_rename_and_identity_change_use_saved_prefix(self):
        title = ""
        terminal_title = "OC | Original title"

        def command(args, **kwargs):
            nonlocal title
            if args[1] == "get-window":
                return subprocess.CompletedProcess(args, 0, json.dumps({"success": True, "id": "own",
                    "display_name": title or terminal_title, "custom_name": title, "title": terminal_title}))
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
            terminal_title = "OC | New conversation"
            pane_identity.update_title(self.paths, "Bob", {"pane_title": title}, private_dir=app.private_dir,
                                       lock=app.FileLock, atomic_json=app.atomic_json, run=run)
            self.assertEqual(title, "[Bob] OC | New conversation")
            title = "User renamed this"
            pane_identity.update_title(self.paths, "Bob", {"pane_title": title}, private_dir=app.private_dir,
                                       lock=app.FileLock, atomic_json=app.atomic_json, run=run)
            self.assertEqual(title, "[Bob] OC | New conversation")
            title = "[Bob] User edited the suffix"
            pane_identity.update_title(self.paths, "Bob", {"pane_title": title}, private_dir=app.private_dir,
                                       lock=app.FileLock, atomic_json=app.atomic_json, run=run)
            self.assertEqual(title, "[Bob] OC | New conversation")

    def test_legacy_prefix_uses_live_title_without_full_name_tracking(self):
        with patch.dict(os.environ, self.pane_env), patch.object(pane_identity, "inventory", return_value={"own"}):
            app.pane_binding(self.paths, app.load_config(self.paths.config))
            filename = self.paths.state / "pane-identities.json"
            state = json.loads(filename.read_text())
            saved = state["bindings"][pane_identity.owner()[0]]
            saved["title_prefix"] = "[Alice] "
            saved["title_name"] = "[Alice] OC | Old conversation"
            app.atomic_json(filename, state)
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                "success": True, "id": "own", "title": "OC | Current conversation",
                "custom_name": "[Alice] OC | Old conversation", "display_name": "[Alice] OC | Old conversation"})))
            pane_identity.update_title(self.paths, "Bob", {"pane_title": "old"}, private_dir=app.private_dir,
                                       lock=app.FileLock, atomic_json=app.atomic_json, run=run)
            self.assertEqual(run.call_args_list[-1].args[0][-1], "[Bob] OC | Current conversation")
            saved = json.loads(filename.read_text())["bindings"][pane_identity.owner()[0]]
            self.assertNotIn("title_name", saved)

    def test_existing_custom_name_is_normalized_from_live_title(self):
        with patch.dict(os.environ, self.pane_env), patch.object(pane_identity, "inventory", return_value={"own"}):
            app.pane_binding(self.paths, app.load_config(self.paths.config))
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                "success": True, "id": "own", "title": "OC | Task", "custom_name": "My pane"})))
            pane_identity.update_title(self.paths, "Alice", {"pane_title": "My pane"}, private_dir=app.private_dir,
                                       lock=app.FileLock, atomic_json=app.atomic_json, run=run)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[-1].args[0][-1], "[Alice] OC | Task")

    def test_missing_custom_name_means_pane_has_not_been_named(self):
        with patch.dict(os.environ, self.pane_env), patch.object(pane_identity, "inventory", return_value={"own"}):
            app.pane_binding(self.paths, app.load_config(self.paths.config))
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                "success": True, "id": "own", "title": "OC | Task", "display_name": "OC | Task"})))
            pane_identity.update_title(self.paths, "Alice", {"pane_title": "OC | Task"}, private_dir=app.private_dir,
                                       lock=app.FileLock, atomic_json=app.atomic_json, run=run)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[-1].args[0][-1], "[Alice] OC | Task")

    def test_both_window_identifier_formats_allow_naming_only_the_owner(self):
        with patch.dict(os.environ, self.pane_env), patch.object(pane_identity, "inventory", return_value={"own"}):
            app.pane_binding(self.paths, app.load_config(self.paths.config))
            for identifiers in ({"id": "own"}, {"window_id": "own"}, {"id": "own", "window_id": "own"}):
                with self.subTest(identifiers=identifiers):
                    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                        "success": True, "title": "Worker task", **identifiers})))
                    pane_identity.update_title(self.paths, "Alice", {"pane_title": "Worker task"},
                                               private_dir=app.private_dir, lock=app.FileLock,
                                               atomic_json=app.atomic_json, run=run)
                    self.assertEqual(run.call_count, 2)
                    command = run.call_args_list[-1].args[0]
                    self.assertEqual(command[1], "set-window")
                    self.assertEqual(command[command.index("-w") + 1], "own")
                    self.assertEqual(command[-1], "[Alice] Worker task")

    def test_missing_wrong_or_conflicting_window_identifiers_block_naming(self):
        with patch.dict(os.environ, self.pane_env), patch.object(pane_identity, "inventory", return_value={"own"}):
            app.pane_binding(self.paths, app.load_config(self.paths.config))
            for identifiers in ({}, {"id": "other"}, {"window_id": "other"},
                                {"id": "own", "window_id": "other"},
                                {"id": "other", "window_id": "own"}):
                with self.subTest(identifiers=identifiers):
                    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                        "success": True, "title": "Task", **identifiers})))
                    with self.assertRaisesRegex(ValueError, "Cannot verify"):
                        pane_identity.update_title(self.paths, "Alice", {"pane_title": "Task"},
                                                   private_dir=app.private_dir, lock=app.FileLock,
                                                   atomic_json=app.atomic_json, run=run)
                    self.assertEqual(run.call_count, 1)

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
