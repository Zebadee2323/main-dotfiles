"""One-shot history, including filters and removed live-mode flags."""
import json
import os

import message_history
from test_agent_comms import Fixture


def record(text, name="Alice", **extra):
    return {"id": name, "name": name, "msg": text, "created_at": "2026-10-31T10:00:00+00:00", **extra}


class HistoryTests(Fixture):
    def write_history(self, *records):
        self.paths.state.mkdir(parents=True, exist_ok=True)
        path = self.paths.state / "history.jsonl"
        temp = path.with_suffix(".tmp")
        temp.write_text("".join(json.dumps(r) + "\n" for r in records))
        os.replace(temp, path)

    def test_removed_live_flags_and_limit_validation(self):
        for args in (["--watch"], ["--refresh-time", "1"], ["--no-animation"]):
            result = self.cli("history", *args)
            self.assertEqual(result.returncode, 2)
            self.assertIn('unrecognized arguments', result.stderr)
        result = self.cli('history', '--limit', '0')
        self.assertEqual(result.returncode, 1)
        self.assertIn('--limit', result.stderr)
        result = self.cli('watch', '--view', 'log')
        self.assertEqual(result.returncode, 1)
        self.assertIn('requires a terminal', result.stderr)

    def test_filters_and_limit_work_without_listener(self):
        self.write_history(record("one"), record("two", "Ben"), record("three", epoch="e1"), record("four"))
        result = self.cli('history', '--name', 'Alice', '--limit', '2', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([m['msg'] for m in json.loads(result.stdout)], ['three','four'])
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
