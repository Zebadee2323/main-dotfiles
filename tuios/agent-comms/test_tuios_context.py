"""Read-only pane discovery must not depend on inherited session names."""
import json
import subprocess
import unittest
from unittest.mock import Mock

import tuios_context as context


class SessionDiscoveryTests(unittest.TestCase):
    env = {"TUIOS_ENV": "1", "TUIOS_PANE_ID": "own", "TUIOS_SESSION": "old-name"}

    def result(self, value):
        return subprocess.CompletedProcess([], 0, json.dumps(value))

    def inventory(self, panes=None):
        panes = [{"window_id": "own", "session": "renamed"},
                 {"window_id": "unrelated", "session": "old-name"}] if panes is None else panes
        return {"success": True, "total": len(panes), "panes": panes}

    def test_rename_or_move_uses_exact_pane_and_never_queries_old_session(self):
        for env in (self.env, {k: v for k, v in self.env.items() if k != "TUIOS_SESSION"}):
            with self.subTest(env=env):
                original = dict(env)
                run = Mock(side_effect=[self.result(self.inventory()),
                    self.result({"session_name": "renamed", "tui_attached": True}),
                    self.result({"windows": [{"window_id": "own", "workspace": 1}],
                                 "current_workspace": 1, "focused_window_id": "own"})])
                source = context.context("", env=env, run=run, strict=True)
                self.assertEqual((source["pane_id"], source["session_name"]), ("own", "renamed"))
                self.assertEqual([call.args[0][1:] for call in run.call_args_list], [
                    ["list-windows", "--all", "--json"],
                    ["session-info", "-s", "renamed", "--json"],
                    ["list-windows", "-s", "renamed", "--json"],
                ])
                self.assertEqual(env, original)

    def test_missing_or_duplicate_pane_never_selects_another_session(self):
        for panes in ([], [{"window_id": "unrelated", "session": "old-name"}],
                      [{"window_id": "own", "session": "a"}, {"window_id": "own", "session": "b"}]):
            run = Mock(return_value=self.result(self.inventory(panes)))
            with self.assertRaisesRegex(context.ContextError, "not found or is ambiguous"):
                context.context("", env=self.env, run=run, strict=True)
            run.assert_called_once()

    def test_incomplete_inventory_or_denial_fails_without_other_queries(self):
        for data in ({**self.inventory(), "total": 99}, {**self.inventory(), "success": False},
                     {**self.inventory(), "errors": ["forbidden"]},
                     self.inventory([{"window_id": "own"}])):
            run = Mock(return_value=self.result(data))
            with self.assertRaises(context.ContextError):
                context.context("", env=self.env, run=run, strict=True)
            run.assert_called_once()
        run = Mock(side_effect=PermissionError("forbidden"))
        with self.assertRaisesRegex(context.ContextError, "access denied"):
            context.context("", env=self.env, run=run, strict=True)
        run.assert_called_once()

    def test_pane_must_still_exist_in_resolved_session(self):
        run = Mock(side_effect=[self.result(self.inventory()),
            self.result({"tui_attached": True}),
            self.result({"windows": [{"window_id": "unrelated", "workspace": 1}],
                         "current_workspace": 1})])
        with self.assertRaisesRegex(context.ContextError, "sender pane was not found"):
            context.context("", env=self.env, run=run, strict=True)
        self.assertEqual(run.call_count, 3)
