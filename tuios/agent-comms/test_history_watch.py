"""history --watch: snapshot, change-only redraws, file replacement, and clean exit."""
import io
import json
import os
from unittest.mock import patch

import agent_comms as app
import message_history
from test_agent_comms import Fixture


class FakeTerminal(io.StringIO):
    def isatty(self):
        return True


def record(text, name="Alice", **extra):
    return {"id": name, "name": name, "msg": text, "created_at": "2026-10-31T10:00:00+00:00", **extra}


class HistoryWatchTests(Fixture):
    def write_history(self, *records):
        self.paths.state.mkdir(parents=True, exist_ok=True)
        path = self.paths.state / "history.jsonl"
        temp = path.with_suffix(".tmp")
        temp.write_text("".join(json.dumps(r) + "\n" for r in records))
        os.replace(temp, path)  # Atomic replacement, like the listener.

    def test_option_validation(self):
        for args, error in ((["--refresh-time", "1"], "requires --watch"),
                            (["--watch", "--refresh-time", "0"], "positive"),
                            (["--watch", "--refresh-time", "-1"], "positive"),
                            (["--watch", "--refresh-time", "nan"], "positive"),
                            (["--watch", "--refresh-time", "inf"], "positive"),
                            (["--watch", "--json"], "--json or --raw"),
                            (["--watch", "--raw"], "--json or --raw"),
                            (["--watch"], "requires a terminal"),
                            (["--limit", "0"], "--limit")):
            with self.subTest(args=args):
                result = self.cli("history", *args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(error, result.stderr)
        self.assertEqual(self.cli("history", "--name", "A", "--id", "A").returncode, 2)

    def test_watch_redraws_only_on_change_and_survives_replacement_and_absence(self):
        terminal = FakeTerminal()
        steps = []
        sizes = iter([(80, 24)] * 4 + [(60, 24)] * 10)

        def sleep(seconds):
            steps.append(seconds)
            step = len(steps)
            if step == 1:
                self.write_history(record("first"))
            elif step == 3:
                self.write_history(record("first"), record("second"))  # Replaced, not appended.
            elif step == 5:
                (self.paths.state / "history.jsonl").unlink()
            elif step == 6:
                raise KeyboardInterrupt

        with patch("sys.stdout", terminal):
            result = app.history_watch(self.paths, limit=10, history_size=10, refresh=0.5, color=False,
                                       filters={}, sleep=sleep, size=lambda: next(sizes))
        self.assertEqual(result, 0)
        self.assertEqual(set(steps), {0.5})
        output = terminal.getvalue()
        frames = output.split("\x1b[H")[1:]
        # Empty, +first, (unchanged: no redraw), +second, resize, absent → empty again.
        self.assertEqual(len(frames), 5, frames)
        self.assertIn("No messages", frames[0])
        self.assertIn("first", frames[1])
        self.assertIn("second", frames[2])
        self.assertIn("second", frames[3])
        self.assertNotEqual(frames[2], frames[3])
        self.assertIn("No messages", frames[4])
        self.assertTrue(output.startswith("\x1b[?1049h"))
        self.assertTrue(output.endswith("\x1b[?25h\x1b[?1049l"), "terminal restored on Ctrl-C")
        self.assertNotIn("\x1b[3J", output, "never clear scrollback")

    def test_watch_applies_filters_and_limit_without_listener(self):
        self.write_history(record("one"), record("two", "Ben"), record("three", epoch="e1"), record("four"))
        terminal = FakeTerminal()
        with patch("sys.stdout", terminal):
            app.history_watch(self.paths, limit=2, history_size=10, refresh=1, color=False,
                              filters={"name": "Alice"}, sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt),
                              size=lambda: (80, 24))
        output = terminal.getvalue()
        self.assertIn("three", output)
        self.assertIn("four", output)
        self.assertNotIn("one", output.replace("Agent", ""))
        self.assertNotIn("two", output)
        self.assertFalse((self.paths.runtime / "server.json").exists())

    def test_name_epoch_and_legacy_id_filters(self):
        self.write_history(record("legacy", "Will", id="ag_1"), record("old", epoch="e1"),
                           record("new", epoch="e2"))
        read = lambda **f: [r["msg"] for r in message_history.read(self.paths.state, 10, **f)]
        self.assertEqual(read(name="Alice"), ["old", "new"])
        self.assertEqual(read(name="Alice", epoch="e2"), ["new"])
        self.assertEqual(read(identity="ag_1"), ["legacy"])
        result = self.cli("history", "--id", "Alice", "--raw")
        self.assertEqual(result.returncode, 0)
        self.assertIn("deprecated", result.stderr)
        self.assertIn("new", result.stdout)
