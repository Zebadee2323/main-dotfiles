import io
import unittest
from unittest.mock import patch

import agent_comms as app
import conversation as c
import history_view
import tree_view


class WatcherHeaderTests(unittest.TestCase):
    def views(self):
        convo = c.ConversationView(color=False)
        tree = tree_view.TreeView("demo", color=False)
        log = history_view.LogView(color=False)
        tree.metadata({"trees": {"demo": {"title": "My project", "nodes": []}}}, [])
        return convo, tree, log

    def test_all_pane_views_share_brand_placement_and_activity(self):
        views = self.views()
        for view in views:
            view.offline = False
            view.message = {"name": "Alice", "phase": "displaying", "kind": "query", "msg": "Hello"}
        headers = [view.render(100, 5, 0)[0] for view in views]
        self.assertEqual([line[:14] for line in headers], [" ◉ agent-comms"] * 3)
        for header, label in zip(headers, ("convo", "tree · My project", "log")):
            self.assertEqual(header[15:15 + len(label)], label)
            self.assertIn("● speaking", header)
            self.assertIn("QUERY", header)
        for view in views:
            view.offline = True
            view.message = None
            self.assertIn("✕ offline", view.render(100, 5, 0)[0])

    def test_header_styles_and_narrow_clipping_are_shared(self):
        for width in (1, 8, 16, 24, 40, 100):
            canvas = c.Canvas(width, 1)
            c.view_header(canvas, width, "tree · " + "long title " * 20,
                          {"phase": "preparing", "kind": "statement"})
            self.assertEqual(sum(c.char_width(char) for char in canvas.lines(False)[0]), width)
            if width >= 14:
                self.assertEqual(canvas.lines(False)[0][:14], " ◉ agent-comms")
            if width > 1:
                self.assertEqual(canvas.cells[0][1][1], f"1;{c.HEADER}")

    def test_convo_is_the_selector_and_dock_default_is_unchanged(self):
        self.assertEqual(app.parser().parse_args(["watch", "--view", "convo"]).view, "convo")
        self.assertEqual(app.parser().parse_args(["watch"]).view, "captions")
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            app.parser().parse_args(["watch", "--view", "conversation"])
