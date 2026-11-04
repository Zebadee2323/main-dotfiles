import asyncio
import io
import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

import agent_comms as app
import pane_identity
from test_agent_comms import Fixture, TEST_PANE, seed_binding


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

    def test_volume_cycle_wraps_without_muting_for_all_watchers(self):
        self.start_server()
        watchers = [self.request("watch")[1] for _ in range(2)]
        for stream in watchers:
            self.assertEqual(json.loads(stream.readline())["audio"]["volume"], 100)
        for volume, muted in ((10, False), (25, False), (50, False),
                              (100, False), (10, False)):
            status = self.audio("cycle-volume")
            self.assertEqual(status["volume"], volume)
            self.assertEqual(status["muted"], muted)
            self.assertIsNone(status["remaining_seconds"])
            if muted:
                self.assertEqual(status["volume_notice_until"], 0)
            for stream in watchers:
                self.assertEqual(json.loads(stream.readline())["audio"], status)

    def test_cycle_cancels_timed_mute_and_permanent_mute_rejects_speech(self):
        self.start_server()
        self.audio("mute", seconds=0.1)
        status = self.audio("cycle-volume")
        self.assertFalse(status["muted"])
        self.assertEqual(status["volume"], 10)
        self.assertIsNone(status["remaining_seconds"])
        for _ in range(4):
            self.audio("cycle-volume")
        self.audio("mute")
        # The old timeout must not unmute an explicit permanent mute.
        import time
        time.sleep(0.15)
        self.assertTrue(self.audio("status")["muted"])
        _, watcher = self.request("watch")
        watcher.readline()
        self.transport_send("Alice", "hello")
        message = json.loads(watcher.readline())["message"]
        _, claim = self.request("claim-speech", uid=message["uid"])
        self.assertFalse(json.loads(claim.readline())["claimed"])

    def test_dock_click_routes_mouse_buttons(self):
        self.start_server()
        # mute: None (unmuted), "snooze" (timed) or "full" (indefinite).
        cases = (("3", False, True, 100, None), ("2", False, True, 100, None),
                 ("1", False, False, 100, "snooze"), ("1", True, False, 10, None),
                 ("1", False, True, 10, "full"), ("1", False, False, 10, "full"),
                 ("1", False, True, 10, None), ("1", True, True, 10, "full"),
                 ("1", True, False, 10, None), ("1", True, False, 25, None),
                 ("1", False, False, 25, "snooze"), ("1", False, True, 25, "full"))
        for button, ctrl, alt, volume, mute in cases:
            with patch.dict(os.environ, {**self.env, "TUIOS_CLICK_BUTTON": button}), \
                 patch.object(app, "control_pressed", return_value=ctrl), \
                 patch.object(app, "alt_pressed", return_value=alt), \
                 patch("sys.stdout", new_callable=io.StringIO):
                result = app.main(["audio", "dock-click"])
            self.assertEqual(result, 0)
            status = self.audio("status")
            self.assertEqual(status["volume"], volume)
            self.assertEqual(status["muted"], mute is not None)
            if mute == "snooze":
                self.assertGreater(status["remaining_seconds"], 0)
            elif mute == "full":
                self.assertIsNone(status["remaining_seconds"])

    def test_macos_control_modifier_mask(self):
        quartz = Mock()
        with patch.object(app.sys, "platform", "darwin"), \
             patch("ctypes.CDLL", return_value=quartz):
            for flags, expected in ((0, False), (1 << 19, False), (1 << 17, False), (1 << 18, True),
                                    ((1 << 18) | (1 << 19), True)):
                quartz.CGEventSourceFlagsState.return_value = flags
                self.assertEqual(app.control_pressed(), expected)
                self.assertEqual(app.alt_pressed(), bool(flags & (1 << 19)))
                quartz.CGEventSourceFlagsState.assert_called_with(0)

    def test_unmute_cancels_old_timer_and_muting_releases_a_speech_lease(self):
        self.start_server()
        _, watcher = self.request("watch")
        watcher.readline()
        self.transport_send("Alice", "hello")
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
    owner_env = {**TEST_PANE, "TUIOS_PANE_ID": "own", "TUIOS_SESSION": "work"}

    def bind(self, name="Alice"):
        """Bind the fake pane to a name, as context would, without querying tuios."""
        seed_binding(self.paths, self.owner_env, name)

    def update(self, name, title, run):
        pane_identity.update_title(self.paths, name, {"pane_title": title}, config=app.load_config(self.paths.config),
                                   private_dir=app.private_dir, lock=app.FileLock, atomic_json=app.atomic_json, run=run)

    def test_known_agent_prefixes_are_normalized_and_other_labels_preserved(self):
        self.assertEqual(pane_identity.prefixed_title("OC | Fix bug", "[Alice] "), "[Alice] OC | Fix bug")
        self.assertEqual(pane_identity.prefixed_title("[Alice] OC | New task", "[Bob] ", "[Alice] "),
                         "[Bob] OC | New task")
        self.assertEqual(pane_identity.prefixed_title("[Alice] OC", "[Alice] ", "[Alice] "), "[Alice] OC")
        self.assertEqual(pane_identity.prefixed_title("[Human] My title", "[Bob] ", "[Alice] "),
                         "[Bob] [Human] My title")
        pattern, _ = pane_identity.title_names(app.load_config(self.paths.config))
        self.assertEqual(pane_identity.prefixed_title("[Alice] [Bob2] OC | Task", "[Bob] ", pattern=pattern),
                         "[Bob] OC | Task")
        self.assertEqual(pane_identity.prefixed_title("[Work] OC | Task", "[Bob] ", pattern=pattern),
                         "[Bob] [Work] OC | Task")
        # Retired literal prefixes (e.g. from before a reset) are also recognized.
        self.assertEqual(pane_identity.prefixed_title("[Zed] OC | Task", "[Bob] ", names=["[Zed] "]),
                         "[Bob] OC | Task")
        self.assertEqual(pane_identity.prefixed_title("[Zed] OC | Task", "[Bob] ", pattern=pattern),
                         "[Bob] [Zed] OC | Task")

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
        with patch.dict(os.environ, self.owner_env):
            self.bind("Alice")
            for name in ("Alice", "Alice", "Bob"):
                if name == "Bob":
                    self.bind("Bob")  # The pane's binding changed (e.g. cleared and reallocated).
                self.update(name, title, run)
            self.assertEqual(title, "[Bob] OC | Original title")
            self.assertEqual(sum(c.args[0][1] == "set-window" for c in run.call_args_list), 2)
            terminal_title = "OC | New conversation"
            self.update("Bob", title, run)
            self.assertEqual(title, "[Bob] OC | New conversation")
            title = "User renamed this"
            self.update("Bob", title, run)
            self.assertEqual(title, "[Bob] OC | New conversation")
            title = "[Bob] User edited the suffix"
            self.update("Bob", title, run)
            self.assertEqual(title, "[Bob] OC | New conversation")

    def test_saved_prefix_uses_live_title_without_full_name_tracking(self):
        with patch.dict(os.environ, self.owner_env):
            self.bind("Bob")
            filename = self.paths.state / "allocation.json"
            state = json.loads(filename.read_text())
            # A saved prefix outside the name catalog is still stripped from the live title.
            state["bindings"][pane_identity.owner()[0]]["title_prefix"] = "[Zed] "
            app.atomic_json(filename, state)
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                "success": True, "id": "own", "title": "[Zed] OC | Current conversation",
                "custom_name": "[Zed] OC | Old conversation", "display_name": "[Zed] OC | Old conversation"})))
            self.update("Bob", "old", run)
            self.assertEqual(run.call_args_list[-1].args[0][-1], "[Bob] OC | Current conversation")
            saved = json.loads(filename.read_text())["bindings"][pane_identity.owner()[0]]
            self.assertEqual(saved["title_prefix"], "[Bob] ")
            self.assertNotIn("title_name", saved)

    def test_title_update_refuses_a_name_the_pane_is_not_bound_to(self):
        with patch.dict(os.environ, self.owner_env):
            self.bind("Alice")
            run = Mock()
            with self.assertRaisesRegex(ValueError, "binding changed"):
                self.update("Bob", "OC | Task", run)
            run.assert_not_called()

    def test_existing_custom_name_is_normalized_from_live_title(self):
        with patch.dict(os.environ, self.owner_env):
            self.bind()
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                "success": True, "id": "own", "title": "OC | Task", "custom_name": "My pane"})))
            self.update("Alice", "My pane", run)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[-1].args[0][-1], "[Alice] OC | Task")

    def test_missing_custom_name_means_pane_has_not_been_named(self):
        with patch.dict(os.environ, self.owner_env):
            self.bind()
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                "success": True, "id": "own", "title": "OC | Task", "display_name": "OC | Task"})))
            self.update("Alice", "OC | Task", run)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[-1].args[0][-1], "[Alice] OC | Task")

    def test_both_window_identifier_formats_allow_naming_only_the_owner(self):
        with patch.dict(os.environ, self.owner_env):
            self.bind()
            for identifiers in ({"id": "own"}, {"window_id": "own"}, {"id": "own", "window_id": "own"}):
                with self.subTest(identifiers=identifiers):
                    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                        "success": True, "title": "Worker task", **identifiers})))
                    self.update("Alice", "Worker task", run)
                    self.assertEqual(run.call_count, 2)
                    command = run.call_args_list[-1].args[0]
                    self.assertEqual(command[1], "set-window")
                    self.assertEqual(command[command.index("-w") + 1], "own")
                    self.assertEqual(command[-1], "[Alice] Worker task")

    def test_missing_wrong_or_conflicting_window_identifiers_block_naming(self):
        with patch.dict(os.environ, self.owner_env):
            self.bind()
            for identifiers in ({}, {"id": "other"}, {"window_id": "other"},
                                {"id": "own", "window_id": "other"},
                                {"id": "other", "window_id": "own"}):
                with self.subTest(identifiers=identifiers):
                    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
                        "success": True, "title": "Task", **identifiers})))
                    with self.assertRaisesRegex(ValueError, "Cannot verify"):
                        self.update("Alice", "Task", run)
                    self.assertEqual(run.call_count, 1)

    def test_naming_failure_does_not_break_context_json_or_retry(self):
        source = {"pane_id": "own", "pane_title": "Task", "introduce": False}
        with patch.dict(os.environ, self.owner_env), patch.object(pane_identity, "inventory", return_value={"own"}), \
             patch.object(app, "checked_context", return_value=source), \
             patch.object(pane_identity.subprocess, "run", side_effect=PermissionError("denied")) as run, \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            result = app.identify(self.paths, app.load_config(self.paths.config))
        self.assertEqual(set(result), {"id", "name", "epoch", "prefix"})
        self.assertEqual(result["id"], result["name"])
        self.assertIn(result["name"], ("Alice", "Bob", "Cleo"))
        self.assertIn("pane name unchanged", errors.getvalue())
        self.assertEqual(run.call_count, 1)
