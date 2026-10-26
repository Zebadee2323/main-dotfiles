from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import subprocess
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
import pane_identity
import tuios_context
from test_agent_comms import Fixture, SCRIPT


class PaneIdentityTests(Fixture):
    def setUp(self):
        super().setUp()
        self.pane_env = {"TUIOS_ENV": "1", "TUIOS_PANE_ID": "own", "TUIOS_SESSION": "work",
                         "TUIOS_SOCKET": "/example/tuios.sock", "TUIOS_HOST": "host"}
        self.source = {"session_name": "work", "pane_id": "own", "workspace": 1, "introduce": False}
        self.config = app.load_config(self.paths.config)

    def bind(self, pane="own", *, allocate=True):
        with patch.dict(os.environ, {**self.pane_env, "TUIOS_PANE_ID": pane}), \
             patch.object(pane_identity, "inventory", return_value={"own", "other", "third", "fourth"}):
            return app.pane_binding(self.paths, self.config, allocate=allocate)

    def test_same_pane_across_harnesses_sessions_moves_and_restarts(self):
        with patch.dict(os.environ, self.pane_env), patch.object(tuios_context, "context", return_value=self.source), \
             patch.object(pane_identity, "inventory", return_value={"own"}):
            first = app.identify(self.paths, self.config)
            second = app.identify(self.paths, self.config)
            self.assertEqual(first, second)
            self.assertEqual(set(first), {"id", "name", "prefix"})
            bound = app.pane_binding(self.paths, self.config, allocate=False)
            self.assertEqual(first["id"], bound)
            self.assertFalse((self.paths.state / "sessions").exists())
            before = (self.paths.state / "bag.json").read_text()
            with patch.dict(os.environ, {"TUIOS_SESSION": "renamed"}):
                self.assertEqual(app.identify(self.paths, self.config), first)
            self.assertEqual((self.paths.state / "bag.json").read_text(), before)
            # A fresh process can read the same binding, without daemon queries.
            result = subprocess.run([os.sys.executable, "-c",
                "import agent_comms as a; p=a.Paths.resolve(a.parser().parse_args(['context'])); "
                "print(a.pane_binding(p,a.load_config(p.config),allocate=False))"],
                cwd=SCRIPT.parent, env={**self.env, **self.pane_env}, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), bound)

    def test_different_panes_get_distinct_identities_and_roster_exhaustion_is_explicit(self):
        identities = {self.bind(p) for p in ("own", "other", "third")}
        self.assertEqual(identities, {"a", "b", "c"})
        with self.assertRaisesRegex(app.CommsError, "No unassigned"):
            self.bind("fourth")

    def test_concurrent_resolution_draws_once(self):
        with patch.dict(os.environ, self.pane_env), patch.object(pane_identity, "inventory", return_value={"own"}):
            with ThreadPoolExecutor(max_workers=8) as pool:
                ids = list(pool.map(lambda _: app.pane_binding(self.paths, self.config), range(16)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(json.loads((self.paths.state / "bag.json").read_text())["remaining"]), 2)
        self.assertEqual(len(json.loads((self.paths.state / "pane-identities.json").read_text())["bindings"]), 1)

    def test_removed_roster_identity_reassigns_but_corrupt_map_does_not_draw(self):
        first = self.bind()
        self.config["identities"].pop(first)
        self.assertNotEqual(self.bind(), first)
        before = (self.paths.state / "bag.json").read_text()
        (self.paths.state / "pane-identities.json").write_text("not json")
        with self.assertRaisesRegex(app.CommsError, "Corrupt pane"):
            self.bind()
        self.assertEqual((self.paths.state / "bag.json").read_text(), before)

    def test_send_checks_expected_identity_and_rejects_unresolved_panes(self):
        with patch.dict(os.environ, self.pane_env), patch.object(tuios_context, "context", return_value=self.source), \
             patch.object(app, "Paths") as paths, patch.object(app, "send", new=AsyncMock()) as send, \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            paths.resolve.return_value = self.paths
            self.assertEqual(app.main(["send", "--id", "a", "--msg", "Ready."]), 1)
            self.assertIn("Run 'agent-comms context'", errors.getvalue())
            bound = self.bind()
            send.assert_not_called()
            self.assertEqual(app.main(["send", "--id", bound, "--msg", "Ready."]), 0)
            send.assert_awaited_once_with(self.paths, bound, "Ready.", None, self.source)

    def test_cached_id_can_be_reused_but_stale_or_other_pane_id_is_rejected(self):
        bound = self.bind()
        other = self.bind("other")
        with patch.dict(os.environ, self.pane_env), patch.object(tuios_context, "context", return_value=self.source), \
             patch.object(app.Paths, "resolve", return_value=self.paths), \
             patch.object(app, "send", new=AsyncMock()) as send, \
             patch.object(app, "identify") as identify, patch("sys.stderr", new_callable=io.StringIO) as errors:
            for _ in range(2):
                self.assertEqual(app.main(["send", "--id", bound, "--msg", "Ready."]), 0)
            self.assertEqual(send.await_count, 2)
            identify.assert_not_called()
            send.reset_mock()
            for bad in (other, "stale-or-unknown"):
                self.assertEqual(app.main(["send", "--id", bad, "--msg", "Ready."]), 1)
            send.assert_not_called()
            self.assertIn("Identity mismatch", errors.getvalue())
            self.assertIn("Run 'agent-comms context'", errors.getvalue())
            self.assertIn("up-to-date ID, name, and prefix", errors.getvalue())
            self.assertIn("Message was not queued", errors.getvalue())

    def test_send_requires_expected_id(self):
        result = self.cli("send", "--msg", "Ready.")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--id", result.stderr)

    def test_context_failure_does_not_allocate(self):
        with patch.dict(os.environ, self.pane_env), \
             patch.object(tuios_context, "context", side_effect=tuios_context.ContextError("forbidden")):
            with self.assertRaisesRegex(app.CommsError, "forbidden"):
                app.identify(self.paths, self.config)
        self.assertFalse(self.paths.state.exists())

    def test_outside_tuios_and_caller_selected_identity_flags_are_rejected(self):
        result = self.cli("context")
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside a tuios pane", result.stderr)
        self.assertFalse(self.paths.state.exists())
        with self.paths.config.open("a") as stream:
            stream.write('personality = "Obsolete personality that must not be returned"\n')
        config = app.load_config(self.paths.config)
        self.assertTrue(all("personality" not in metadata for metadata in config["identities"].values()))
        for arguments in (("--harness", "codex"), ("--session-id", "one"), ("--id", "a")):
            result = self.cli("context", *arguments)
            self.assertEqual(result.returncode, 2)
            self.assertIn("unrecognized arguments", result.stderr)

    def test_id_changes_spatial_prefix_without_changing_identity(self):
        source = {**self.source, "introduce": True, "position": "top-right", "position_reference": "focused-pane"}
        with patch.dict(os.environ, self.pane_env), patch.object(pane_identity, "inventory", return_value={"own"}), \
             patch.object(tuios_context, "context", return_value=source):
            first = app.identify(self.paths, self.config)
            self.assertEqual(first["prefix"], f"{first['name']} here, to your top-right.")
            source["position"] = "bottom-left"
            second = app.identify(self.paths, self.config)
            self.assertEqual(first["name"], second["name"])
            self.assertNotEqual(first["prefix"], second["prefix"])


class GarbageCollectionTests(Fixture):
    def state(self):
        return {"bindings": {
            "own": {"daemon": "d", "pane_id": "own", "id": "a"},
            "gone": {"daemon": "d", "pane_id": "gone", "id": "b"},
            "other-daemon": {"daemon": "elsewhere", "pane_id": "gone", "id": "c"},
        }}

    def test_confirmed_absence_starts_grace_period_then_prunes(self):
        state = self.state()
        pane_identity.prune(state, "d", "own", {"own"}, 100)
        self.assertEqual(state["bindings"]["gone"]["missing_since"], 100)
        pane_identity.prune(state, "d", "own", {"own"}, 100 + pane_identity.GRACE_SECONDS - 1)
        self.assertIn("gone", state["bindings"])
        pane_identity.prune(state, "d", "own", {"own"}, 100 + pane_identity.GRACE_SECONDS)
        self.assertEqual(set(state["bindings"]), {"own", "other-daemon"})

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
        base = {"TUIOS_ENV": "1", "TUIOS_PANE_ID": "a", "TUIOS_SOCKET": "/socket"}
        self.assertEqual(pane_identity.owner(base), pane_identity.owner({**base, "TUIOS_SESSION": "new"}))
        self.assertNotEqual(pane_identity.owner(base), pane_identity.owner({**base, "TUIOS_SOCKET": "/other"}))
