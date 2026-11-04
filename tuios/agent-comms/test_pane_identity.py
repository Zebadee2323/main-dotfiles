from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import re
import subprocess
import time
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
import allocation
import pane_identity
import shell_identity
import tuios_context
from test_agent_comms import Fixture, SCRIPT


class PaneIdentityTests(Fixture):
    def setUp(self):
        super().setUp()
        # A fake pane on a nonexistent socket: never the user's live tuios daemon.
        self.own_env = {"TUIOS_ENV": "1", "TUIOS_PANE_ID": "own", "TUIOS_SESSION": "work",
                        "TUIOS_SOCKET": "/nonexistent/example/tuios.sock", "TUIOS_HOST": "host"}
        self.source = {"session_name": "work", "pane_id": "own", "workspace": 1, "introduce": False}
        self.config = app.load_config(self.paths.config)
        self.state_file = self.paths.state / allocation.STATE_FILE

    def env_for(self, pane="own", **extra):
        return {**self.own_env, "TUIOS_PANE_ID": pane, **extra}

    def bind(self, pane="own", *, allocate=True, live=("own", "other", "third", "fourth")):
        with patch.object(pane_identity, "inventory", return_value=set(live)):
            return app.pane_binding(self.paths, self.config, allocate=allocate, env=self.env_for(pane))

    def name(self, pane="own", **kwargs):
        return self.bind(pane, **kwargs)[1]["name"]

    def state(self):
        return json.loads(self.state_file.read_text())

    def test_same_pane_across_harnesses_sessions_moves_and_restarts(self):
        with patch.dict(os.environ, self.own_env), patch.object(tuios_context, "context", return_value=self.source), \
             patch.object(pane_identity, "inventory", return_value={"own"}):
            first = app.identify(self.paths, self.config)
            second = app.identify(self.paths, self.config)
            self.assertEqual(first, second)
            self.assertEqual(set(first), {"id", "name", "epoch", "prefix"})
            self.assertEqual(first["id"], first["name"])
            epoch, bound = app.pane_binding(self.paths, self.config, allocate=False)
            self.assertEqual((first["epoch"], first["name"]), (epoch, bound["name"]))
            self.assertFalse((self.paths.state / "sessions").exists())
            before = self.state_file.read_text()
            with patch.dict(os.environ, {"TUIOS_SESSION": "renamed"}):
                self.assertEqual(app.identify(self.paths, self.config), first)
            self.assertEqual(self.state_file.read_text(), before)
            # A fresh process can read the same binding, without daemon queries.
            result = subprocess.run([os.sys.executable, "-c",
                "import json, agent_comms as a; p=a.Paths.resolve(a.parser().parse_args(['context'])); "
                "e, b = a.pane_binding(p, a.load_config(p.config), allocate=False); print(json.dumps([e, b['name']]))"],
                cwd=SCRIPT.parent, env={**self.env, **self.own_env}, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), [epoch, bound["name"]])

    def test_lookup_is_lock_free_and_never_creates_state(self):
        env = self.env_for()
        with patch.object(app, "private_dir", side_effect=AssertionError("mkdir/chmod")), \
             patch.object(app, "FileLock", side_effect=AssertionError("lock")):
            with self.assertRaisesRegex(app.ContextMismatch, "no current agent name"):
                app.pane_binding(self.paths, self.config, allocate=False, env=env)
        self.assertFalse(self.paths.state.exists())
        epoch, bound = self.bind()
        (self.paths.state / allocation.LOCK_FILE).unlink()
        listing = sorted(os.listdir(self.paths.state))
        mode = self.paths.state.stat().st_mode
        with patch.object(app, "private_dir", side_effect=AssertionError("mkdir/chmod")), \
             patch.object(app, "FileLock", side_effect=AssertionError("lock")):
            self.assertEqual(app.pane_binding(self.paths, self.config, allocate=False, env=env), (epoch, bound))
            self.assertEqual(pane_identity.lookup(self.paths, env), (epoch, bound))
        self.assertEqual(sorted(os.listdir(self.paths.state)), listing)
        self.assertEqual(self.paths.state.stat().st_mode, mode)

    def test_different_panes_get_distinct_names_and_names_are_unbounded(self):
        bindings = {pane: self.bind(pane)[1] for pane in ("own", "other", "third", "fourth")}
        names = [binding["name"] for binding in bindings.values()]
        self.assertEqual(set(names[:3]), {"Alice", "Bob", "Cleo"})
        # The bag refills with a numbered generation instead of failing.
        self.assertIn(names[3], {"Alice2", "Bob2", "Cleo2"})
        self.assertEqual(len(set(names)), 4)
        for binding in bindings.values():
            self.assertEqual(binding["voice_profile"]["gender"], binding["gender"])
            self.assertEqual(binding["voice_profile_id"], {"female": "voice_f", "male": "voice_m"}[binding["gender"]])
        self.assertEqual(self.state()["names"]["generation"], 1)

    def test_concurrent_resolution_draws_once(self):
        env = self.env_for()
        with patch.object(pane_identity, "inventory", return_value={"own"}):
            with ThreadPoolExecutor(max_workers=8) as pool:
                names = list(pool.map(lambda _: app.pane_binding(self.paths, self.config, env=env)[1]["name"],
                                      range(16)))
        self.assertEqual(len(set(names)), 1)
        state = self.state()
        self.assertEqual(len(state["names"]["remaining"]), 2)
        self.assertEqual(len(state["bindings"]), 1)

    def test_scan_is_hourly_and_failed_inventory_never_changes_bindings(self):
        for pane in ("own", "other", "third"):
            self.bind(pane)
        original = self.state()
        state = self.state()
        state["scanned"] = {}
        app.atomic_json(self.state_file, state)
        with patch.object(pane_identity, "inventory", return_value=None) as inventory:
            for _ in range(2):
                app.pane_binding(self.paths, self.config, env=self.env_for("other"))
        # Failures are throttled too: never retried within the scan interval.
        inventory.assert_called_once()
        self.assertEqual(self.state()["bindings"], original["bindings"])

    def test_grace_period_prunes_absent_pane_without_recycling_its_name(self):
        for pane in ("own", "other"):
            self.bind(pane)
        old = self.name("other", allocate=False)
        key = pane_identity.owner(self.env_for("other"))[0]
        state = self.state()
        state["scanned"] = {}
        state["bindings"][key]["missing_since"] = time.time() - pane_identity.GRACE_SECONDS - 1
        app.atomic_json(self.state_file, state)
        bags = (state["names"], state["voices"])
        self.bind("own", live={"own"})
        state = self.state()
        self.assertNotIn(key, state["bindings"])
        self.assertEqual((state["names"], state["voices"]), bags)
        with self.assertRaisesRegex(app.ContextMismatch, "no current agent name"):
            self.bind("other", allocate=False)
        self.assertNotEqual(self.name("other"), old)

    def test_clear_only_removes_current_pane_and_does_not_draw(self):
        bound = self.name()
        other = self.bind("other")
        before = self.state()
        env = self.env_for()
        key = pane_identity.owner(env)[0]
        self.assertEqual(app.pane_binding(self.paths, self.config, clear=True, env=env),
                         {"cleared": True, "name": bound})
        self.assertEqual(app.pane_binding(self.paths, self.config, clear=True, env=env),
                         {"cleared": False, "name": None})
        with self.assertRaisesRegex(app.ContextMismatch, "no current agent name"):
            app.pane_binding(self.paths, self.config, allocate=False, env=env)
        del before["bindings"][key]
        self.assertEqual(self.state(), before)
        self.assertEqual(self.bind("other", allocate=False), other)
        # Cleared names stay spent; the next context draws a fresh one.
        self.assertNotIn(self.name(), (bound, other[1]["name"]))

    def test_clear_rejects_unresolved_shell_and_corrupt_state(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(shell_identity, "discover", side_effect=ValueError("No persistent parent shell found")):
            with self.assertRaisesRegex(app.CommsError, "No persistent parent shell"):
                app.pane_binding(self.paths, self.config, clear=True)
        self.assertFalse(self.paths.state.exists())
        self.bind()
        self.state_file.write_text("not json")
        with self.assertRaisesRegex(app.CommsError, "Corrupt allocation state"):
            app.pane_binding(self.paths, self.config, clear=True, env=self.env_for())
        self.assertEqual(self.state_file.read_text(), "not json")

    def test_clear_and_refill_are_mutually_exclusive_and_refill_is_retired(self):
        self.assertEqual(self.cli("context", "--clear", "--refill").returncode, 2)
        result = self.cli("context", "--refill")
        self.assertEqual(result.returncode, 1)
        self.assertIn("retired", result.stderr)
        self.assertIn("bags reset --confirm", result.stderr)
        self.assertFalse(self.paths.state.exists())

    def test_clear_all_offline_across_daemons_preserves_other_state(self):
        first = self.name()
        with patch.object(pane_identity, "inventory", return_value={"own"}):
            second = app.pane_binding(self.paths, self.config,
                                      env=self.env_for(TUIOS_SOCKET="/nonexistent/other/daemon.sock"))[1]["name"]
        before = self.state()
        history = self.paths.state / "history.jsonl"
        history.write_text("keep history")
        source = self.paths.config.read_text()
        # Administrative reset needs neither a pane nor a running listener/config.
        self.paths.config.unlink()
        result = self.cli("context", "--clear-all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"cleared": 2})
        after = self.state()
        self.assertEqual((after["bindings"], after["scanned"]), ({}, {}))
        for field in ("epoch", "names", "voices"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(history.read_text(), "keep history")
        with self.assertRaisesRegex(app.ContextMismatch, "no current agent name"):
            self.bind(allocate=False)
        self.paths.config.write_text(source)
        self.assertNotIn(self.name(), (first, second))

    def test_clear_all_is_idempotent_and_does_not_draw(self):
        for _ in range(2):
            result = self.cli("context", "--clear-all")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"cleared": 0})
        self.assertFalse(self.state_file.exists())

    def test_clear_all_rejects_corrupt_state_and_conflicting_flags(self):
        self.bind()
        self.state_file.write_text("not json")
        result = self.cli("context", "--clear-all")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Corrupt allocation state", result.stderr)
        self.assertEqual(self.state_file.read_text(), "not json")
        for flag in ("--clear", "--refill"):
            self.assertEqual(self.cli("context", "--clear-all", flag).returncode, 2)

    def test_corrupt_or_legacy_state_fails_closed_without_drawing(self):
        self.bind()
        self.state_file.write_text("not json")
        with self.assertRaisesRegex(app.CommsError, "Corrupt allocation state"):
            self.bind("other")
        self.assertEqual(self.state_file.read_text(), "not json")
        self.state_file.unlink()
        (self.paths.state / "pane-identities.json").write_text('{"version": 1, "bindings": {}, "scanned": {}}')
        for allocate in (True, False):
            with self.assertRaisesRegex(app.CommsError, r"bags reset --confirm"):
                self.bind(allocate=allocate)
        self.assertFalse(self.state_file.exists())

    def test_send_checks_expected_name_and_rejects_unresolved_panes(self):
        with patch.dict(os.environ, self.own_env), patch.object(tuios_context, "context", return_value=self.source), \
             patch.object(app, "Paths") as paths, patch.object(app, "send", new=AsyncMock()) as send, \
             patch("sys.stderr", new_callable=io.StringIO) as errors, patch("sys.stdout", new_callable=io.StringIO):
            paths.resolve.return_value = self.paths
            self.assertEqual(app.main(["send", "--name", "Alice", "--epoch", "0" * 16, "--msg", "Ready."]), 1)
            self.assertIn("no current agent name", errors.getvalue())
            self.assertIn("Run 'agent-comms context'", errors.getvalue())
            self.assertFalse(self.state_file.exists())
            epoch, bound = self.bind()
            send.assert_not_called()
            self.assertEqual(app.main(["send", "--name", bound["name"], "--epoch", epoch, "--msg", "Ready."]), 0)
            send.assert_awaited_once()
            self.assertEqual(send.await_args.args[:6], (self.paths, bound["name"], epoch, "Ready.", None, self.source))

    def test_cached_name_can_be_reused_but_stale_or_other_pane_name_is_rejected(self):
        epoch, bound = self.bind()
        other = self.name("other")
        name = bound["name"]
        with patch.dict(os.environ, self.own_env), patch.object(tuios_context, "context", return_value=self.source), \
             patch.object(app.Paths, "resolve", return_value=self.paths), \
             patch.object(app, "send", new=AsyncMock(return_value={"message_id": "f" * 32})) as send, \
             patch.object(app, "identify") as identify, patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            for _ in range(2):
                self.assertEqual(app.main(["send", "--name", name, "--epoch", epoch, "--msg", "Ready."]), 0)
            self.assertEqual(send.await_count, 2)
            self.assertIn("Message ID: " + "f" * 32, output.getvalue())
            identify.assert_not_called()
            # The deprecated --id alias still accepts the generated name.
            self.assertEqual(app.main(["send", "--id", name, "--epoch", epoch, "--msg", "Ready."]), 0)
            self.assertIn("--id is deprecated", errors.getvalue())
            send.reset_mock()
            fresh = {"id": name, "name": name, "epoch": epoch, "prefix": ""}
            with patch.object(app, "listener_context", new=AsyncMock(return_value=fresh)) as refresh:
                for arguments, error in ((["--name", other, "--epoch", epoch], "Name mismatch"),
                                         (["--name", "Stale", "--epoch", epoch], "Name mismatch"),
                                         (["--name", name, "--epoch", "0" * 16], "Epoch mismatch"),
                                         (["--name", name], "Missing --epoch")):
                    with self.subTest(arguments=arguments):
                        errors.seek(0)
                        errors.truncate()
                        self.assertEqual(app.main(["send", *arguments, "--msg", "Ready."]), 1)
                        self.assertIn(error, errors.getvalue())
                        self.assertIn("Fresh context: " + json.dumps(fresh, separators=(",", ":")), errors.getvalue())
                        self.assertIn("up-to-date name, epoch, and prefix", errors.getvalue())
                        self.assertIn("Message was not queued", errors.getvalue())
                self.assertEqual(refresh.await_count, 4)
            send.assert_not_called()

    def test_send_requires_expected_name(self):
        result = self.cli("send", "--msg", "Ready.")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--name NAME --epoch EPOCH", result.stderr)
        result = self.cli("send", "--name", "Alice", "--id", "Alice", "--msg", "Ready.")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not allowed with", result.stderr)
        self.assertFalse(self.paths.state.exists())

    def test_context_failure_does_not_allocate(self):
        with patch.dict(os.environ, self.own_env), \
             patch.object(tuios_context, "context", side_effect=tuios_context.ContextError("forbidden")):
            with self.assertRaisesRegex(app.CommsError, "forbidden"):
                app.identify(self.paths, self.config)
        self.assertFalse(self.paths.state.exists())

    def test_offline_standalone_and_caller_selected_identity_flags_are_rejected(self):
        result = self.cli("context")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Listener is offline", result.stderr)
        self.assertFalse(self.paths.state.exists())
        for arguments in (("--harness", "codex"), ("--session-id", "one"), ("--id", "Alice"), ("--name", "Alice")):
            result = self.cli("context", *arguments)
            self.assertEqual(result.returncode, 2)
            self.assertIn("unrecognized arguments", result.stderr)
        # Profiles carry voice settings only; obsolete identity fields are rejected, never returned.
        for field in ('personality = "Obsolete personality"', 'name = "Alice"'):
            with self.subTest(field=field):
                self.paths.config.write_text(self.paths.config.read_text().replace(
                    'gender = "male"\n', f'gender = "male"\n{field}\n'))
                with self.assertRaisesRegex(app.CommsError, "unsupported field"):
                    app.load_config(self.paths.config)
                self.paths.config.write_text(self.paths.config.read_text().replace(f"{field}\n", ""))

    def test_id_changes_spatial_prefix_without_changing_name(self):
        source = {**self.source, "introduce": True, "position": "top-right", "position_reference": "focused-pane"}
        with patch.dict(os.environ, self.own_env), patch.object(pane_identity, "inventory", return_value={"own"}), \
             patch.object(tuios_context, "context", return_value=source):
            first = app.identify(self.paths, self.config)
            self.assertEqual(first["prefix"], f"{first['name']} here, to your top-right.")
            source["position"] = "bottom-left"
            second = app.identify(self.paths, self.config)
            self.assertEqual((first["name"], first["epoch"]), (second["name"], second["epoch"]))
            self.assertNotEqual(first["prefix"], second["prefix"])

    def test_title_prefix_normalizes_generated_names_and_retires_cleared_prefixes(self):
        _, bound = self.bind()
        name = bound["name"]
        env = self.env_for()
        windows = []

        def run(command, **_):
            if command[1] == "get-window":
                return subprocess.CompletedProcess(command, 0, json.dumps(
                    {"success": True, "id": "own", "title": windows.pop(0), "custom_name": ""}))
            return subprocess.CompletedProcess(command, 0, "{}")

        for title, expected in (("[Bob2] [WIP] Task", f"[{name}] [WIP] Task"),
                                (f"[{name}] [Cleo10] OC | Task", f"[{name}] OC | Task"),
                                ("[Alice1] Task", f"[{name}] [Alice1] Task")):
            with self.subTest(title=title):
                windows.append(title)
                calls = Mock(side_effect=run)
                pane_identity.update_title(self.paths, name, {"pane_title": True}, config=self.config,
                                           private_dir=app.private_dir, lock=app.FileLock,
                                           atomic_json=app.atomic_json, run=calls, env=env)
                command = calls.call_args_list[-1].args[0]
                self.assertEqual(command[1], "set-window")
                self.assertEqual(command[command.index("--name") + 1], expected)
        key = pane_identity.owner(env)[0]
        self.assertEqual(self.state()["bindings"][key]["title_prefix"], f"[{name}] ")
        app.pane_binding(self.paths, self.config, clear=True, env=env)
        retired = self.state()["retired_title_prefixes"]
        self.assertEqual(retired, [f"[{name}] "])
        pattern, names = pane_identity.title_names(self.config, retired)
        self.assertEqual(pane_identity.prefixed_title(f"[{name}] [Alice2] [WIP] Task", "[Bob3] ", names=names,
                                                      pattern=pattern), "[Bob3] [WIP] Task")
        self.assertEqual(pane_identity.prefixed_title("[Human] Task", "[Bob3] ", names=names, pattern=pattern),
                         "[Bob3] [Human] Task")
        self.assertIsNone(re.match(pattern, "[Alice1] "))


class GarbageCollectionTests(Fixture):
    def state(self):
        return {"names": {"generation": 0, "remaining": []}, "bindings": {
            "own": {"daemon": "d", "pane_id": "own", "name": "Alice"},
            "gone": {"daemon": "d", "pane_id": "gone", "name": "Bob", "title_prefix": "[Bob] "},
            "other-daemon": {"daemon": "elsewhere", "pane_id": "gone", "name": "Cleo"},
        }}

    def test_confirmed_absence_starts_grace_period_then_prunes_without_recycling(self):
        state = self.state()
        pane_identity.prune(state, "d", "own", {"own"}, 100)
        self.assertEqual(state["bindings"]["gone"]["missing_since"], 100)
        pane_identity.prune(state, "d", "own", {"own"}, 100 + pane_identity.GRACE_SECONDS - 1)
        self.assertIn("gone", state["bindings"])
        pane_identity.prune(state, "d", "own", {"own"}, 100 + pane_identity.GRACE_SECONDS)
        self.assertEqual(set(state["bindings"]), {"own", "other-daemon"})
        # Pruned names never return to the bag; their title prefix is retired.
        self.assertEqual(state["names"], self.state()["names"])
        self.assertEqual(state["retired_title_prefixes"], ["[Bob] "])

    def test_returning_or_detached_panes_are_retained_and_clear_absence(self):
        state = self.state()
        pane_identity.prune(state, "d", "own", {"own"}, 100)
        pane_identity.prune(state, "d", "own", {"own", "gone"}, 1000000)
        self.assertNotIn("missing_since", state["bindings"]["gone"])

    def test_failure_or_partial_listing_never_prunes(self):
        for live in (None, set(), {"another"}):
            state = self.state()
            pane_identity.prune(state, "d", "own", live, 1000000)
            self.assertEqual(state, self.state())
        for response in ({"success": True, "total": 2, "panes": [{"window_id": "own"}]},
                         {"success": True, "total": 0, "panes": [], "errors": ["forbidden"]},
                         {"success": True, "total": 1, "panes": [None]}):
            run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(response)))
            self.assertIsNone(pane_identity.inventory(run))
        run = Mock(side_effect=PermissionError("forbidden"))
        self.assertIsNone(pane_identity.inventory(run))
        self.assertEqual(run.call_count, 1)

    def test_valid_inventory_includes_every_session_and_daemon_key_ignores_session(self):
        data = {"success": True, "total": 2, "panes": [{"window_id": "a", "session": "detached"},
                                                        {"window_id": "b", "session": "active"}]}
        run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(data)))
        self.assertEqual(pane_identity.inventory(run), {"a", "b"})
        base = {"TUIOS_ENV": "1", "TUIOS_PANE_ID": "a", "TUIOS_SOCKET": "/nonexistent/socket"}
        self.assertEqual(pane_identity.owner(base), pane_identity.owner({**base, "TUIOS_SESSION": "new"}))
        self.assertNotEqual(pane_identity.owner(base), pane_identity.owner({**base, "TUIOS_SOCKET": "/nonexistent/other"}))
