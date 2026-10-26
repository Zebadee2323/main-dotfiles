import asyncio
import json
import subprocess
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
import tuios_context as context
from test_agent_comms import Fixture


def pane(identity, x, y, width=40, height=20, workspace=1, **options):
    return dict(window_id=identity, x=x, y=y, width=width, height=height, workspace=workspace, **options)


class GeometryTests(unittest.TestCase):
    def test_eight_relative_directions(self):
        focused = pane("focused", 40, 20)
        for (x, y), expected in {
            (0, 0): "top-left", (40, 0): "top", (80, 0): "top-right",
            (0, 20): "left", (80, 20): "right",
            (0, 40): "bottom-left", (40, 40): "bottom", (80, 40): "bottom-right",
        }.items():
            with self.subTest(expected=expected):
                self.assertEqual(context.relative_position(pane("own", x, y), focused), expected)

    def test_unequal_shared_row_and_column(self):
        self.assertEqual(context.relative_position(pane("own", 40, 0), pane("other", 0, 0, height=40)), "right")
        self.assertEqual(context.relative_position(pane("own", 0, 20), pane("other", 0, 0, width=80)), "bottom")

    def test_overlapping_float_uses_centers_and_identical_bounds_have_no_direction(self):
        self.assertEqual(context.relative_position(pane("own", 10, 10), pane("other", 0, 0)), "bottom-right")
        self.assertIsNone(context.relative_position(pane("own", 0, 0), pane("other", 0, 0)))

    def test_absolute_workspace_regions_and_nonzero_origin(self):
        panes = [pane(str((x, y)), x + 10, y + 5) for x in (0, 40, 80) for y in (0, 20, 40)]
        expected = ["top-left", "left", "bottom-left", "top", "center", "bottom", "top-right", "right", "bottom-right"]
        # Other workspaces and minimized/scratch panes must not distort the bounds.
        others = [pane("other", 0, 0, 1000, 1000, workspace=2),
                  pane("min", 0, 0, 1000, 1000, minimized=True),
                  pane("scratch", 0, 0, 1000, 1000, scratch=True)]
        for own, region in zip(panes, expected):
            self.assertEqual(context.workspace_position(own, panes + others), region)

    def test_unknown_or_nominal_geometry_has_no_position(self):
        other = pane("other", 0, 0)
        for own in ({"workspace": 1}, pane("own", 40, 0, width=0), pane("own", 40, 0, unplaced=True),
                    pane("own", 40, 0, minimized=True), pane("own", float("nan"), 0)):
            self.assertIsNone(context.relative_position(own, other))
            self.assertIsNone(context.workspace_position(own, [own, other]))


class SpatialContextTests(unittest.TestCase):
    def inspect(self, *, current=1, focused="other", attached=True, geometry=True):
        own = pane("own", 40, 0)
        if not geometry:
            own.pop("x")
        listing = {"current_workspace": current, "focused_window_id": focused,
                   "windows": [own, pane("other", 0, 20), pane("third", 0, 0)]}
        run = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps({"tui_attached": attached})),
                                subprocess.CompletedProcess([], 0, json.dumps(listing))])
        return context.context("Alice", run=run, env={"TUIOS_ENV": "1", "TUIOS_SESSION": "work", "TUIOS_PANE_ID": "own"}, strict=True)

    def test_same_workspace_uses_focused_pane_not_workspace_center(self):
        source = self.inspect()
        self.assertEqual(source["position"], "top-right")
        self.assertEqual(source["position_reference"], "focused-pane")

    def test_different_workspace_uses_absolute_position(self):
        source = self.inspect(current=2)
        self.assertEqual(source["position"], "top-right")
        self.assertEqual(source["position_reference"], "workspace")

    def test_focused_has_no_introduction_and_unavailable_geometry_falls_back(self):
        self.assertFalse(self.inspect(focused="own")["introduce"])
        for options in ({"attached": False}, {"geometry": False}, {"focused": "missing"}):
            source = self.inspect(**options)
            self.assertTrue(source["introduce"])
            self.assertNotIn("position", source)


class SpatialPrefixTests(Fixture):
    source = {"session_name": "work", "pane_id": "own", "workspace": 2, "introduce": True,
              "position": "top-right", "position_reference": "focused-pane"}

    def config(self):
        return app.load_config(self.paths.config)

    def test_relative_and_absolute_wording(self):
        self.assertEqual(app.expected_prefix(self.config(), "a", self.source), "Alice here, to your top-right.")
        for position, phrase in (("top-left", "in the top-left"), ("bottom", "at the bottom"),
                                 ("right", "on the right"), ("center", "in the center")):
            source = {**self.source, "position": position, "position_reference": "workspace"}
            self.assertEqual(app.expected_prefix(self.config(), "a", source),
                             f"Alice here, {phrase} of workspace 2, in the work session.")

    def test_invalid_position_falls_back_without_injecting_text(self):
        self.assertEqual(app.expected_prefix(self.config(), "a", {**self.source, "position": "bad\ntext"}),
                         "Alice here, of the work session.")

    def test_duplicate_stale_and_markup_prefixed_introductions_are_rejected(self):
        expected = "Alice here, to your top-right."
        stale = ("Alice here, to your bottom-left.", "Alice here, at the bottom of workspace 3, in the work session.",
                 "Alice here, of the work session.")
        for introduction in (expected, *stale):
            for text, prefix in ((introduction + " Ready.", ""),
                                 (expected + ' <break time="0.3s" /> ' + introduction + " Ready.", expected)):
                with self.subTest(text=text), self.assertRaises(app.CommsError):
                    app.check_prefix("a", prefix, text, text)
        for introduction in stale:
            with self.assertRaises(app.CommsError):
                app.check_prefix("a", expected, introduction + " Ready.", expected + " Ready.")

    def test_focus_or_geometry_change_rejects_before_network(self):
        source = {**self.source, "position": "bottom-left"}
        with patch.object(context, "context", return_value=source), patch.object(app, "send", new=AsyncMock()) as send:
            prefix, checked = app.prefix_context(self.config(), "a")
            with self.assertRaises(app.CommsError):
                app.validate_message(self.config(), "a", "Alice here, to your top-right. Ready.", source=checked)
            self.assertEqual(prefix, "Alice here, to your bottom-left.")
            send.assert_not_called()

    def test_listener_accepts_spatial_prefix_unchanged_and_rejects_stale_one(self):
        self.start_server()
        text = "Alice here, to your top-right. Ready."
        _, stream = self.request("send", id="a", msg=text, tts=text, source=self.source)
        self.assertTrue(json.loads(stream.readline())["ok"])
        _, stream = self.request("send", id="a", msg="Alice here, to your bottom-left. Ready.", source=self.source)
        self.assertFalse(json.loads(stream.readline())["ok"])
        self.assertEqual(json.loads(self.cli("history", "--json").stdout)[0]["msg"], text)

    def test_old_listener_requires_restart_before_spatial_send(self):
        with patch.object(app, "endpoint", return_value={"prefix_policy": app.PREFIX_POLICY}), \
             patch.object(app, "rpc", new=AsyncMock()) as rpc:
            with self.assertRaisesRegex(app.CommsError, "predates spatial prefixes"):
                asyncio.run(app.send(self.paths, "a", "Alice here, to your top-right. Ready.", source=self.source))
            rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
