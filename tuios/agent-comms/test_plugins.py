"""Identity stability and hook contracts; no real model, listener or trust changes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import agent_comms as app
import install_plugins
from test_agent_comms import Fixture, ROOT


class SessionTests(Fixture):
    def assign(self, session="session-one", harness="opencode"):
        result = self.cli("session", "--harness", harness, "--session-id", session)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_assignment_survives_repeated_processes_and_refills(self):
        first = self.assign()
        self.cli("id", "--refill")
        before = (self.paths.state / "bag.json").read_text()
        for _ in range(3):
            self.assertEqual(self.assign(), first)
        self.assertEqual((self.paths.state / "bag.json").read_text(), before)
        self.assertIn(f"--id {first['id']}", first["instructions"])
        self.assertIn("Always close every turn", first["instructions"])
        self.assertIn("immediately BEFORE", first["instructions"])
        self.assertIn("voice mode", first["instructions"])
        self.assertIn("Claude Code", first["instructions"])

    def test_concurrent_same_session_draws_once(self):
        self.cli("id", "--refill")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.cli("session", "--harness", "codex", "--session-id", "same"), range(16)))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        self.assertEqual(len({json.loads(r.stdout)["id"] for r in results}), 1)
        remaining = json.loads((self.paths.state / "bag.json").read_text())["remaining"]
        self.assertEqual(len(remaining), 2)

    def test_new_sessions_and_harnesses_use_distinct_bag_entries(self):
        results = [self.assign("one"), self.assign("two"), self.assign("one", "codex")]
        self.assertEqual({r["id"] for r in results}, {"a", "b", "c"})

    def test_config_metadata_updates_without_redraw(self):
        first = self.assign()
        before = (self.paths.state / "bag.json").read_text()
        text = self.paths.config.read_text().replace(first["name"], "Renamed")
        self.paths.config.write_text(text)
        updated = self.assign()
        self.assertEqual(updated["id"], first["id"])
        self.assertEqual(updated["name"], "Renamed")
        self.assertEqual((self.paths.state / "bag.json").read_text(), before)

    def test_corrupt_assignment_fails_instead_of_consuming_bag(self):
        self.assign()
        record = next((self.paths.state / "sessions").glob("*.json"))
        record.write_text("not json")
        before = (self.paths.state / "bag.json").read_text()
        result = self.cli("session", "--harness", "opencode", "--session-id", "session-one")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Corrupt session assignment", result.stderr)
        self.assertEqual((self.paths.state / "bag.json").read_text(), before)

    def test_removed_identity_is_reassigned(self):
        first = self.assign()
        self.paths.config.write_text('[identities.only]\nname = "Only"\n')
        second = self.assign()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["id"], "only")


class CodexHookTests(Fixture):
    def setUp(self):
        super().setUp()
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        # Match the supported source-tree launcher installation, without using
        # the user's real executable or resolving their real session identities.
        if os.name == "nt":
            (bin_dir / "agent-comms.cmd").write_text("@echo off\n")
            self.skipTest("Native Windows hook launching is deferred")
        else:
            (bin_dir / "agent-comms").symlink_to(ROOT / "agent-comms")
        self.env["PATH"] = str(bin_dir)
        self.hook = ROOT / "plugins/codex/hooks/session_start.py"

    def invoke(self, event):
        return subprocess.run([sys.executable, str(self.hook)], input=json.dumps(event),
                              capture_output=True, encoding="utf-8", env=self.env, timeout=15)

    def test_codex_start_resume_compact_reuse_assignment(self):
        outputs = []
        for source in ("startup", "resume", "compact", "clear"):
            result = self.invoke({"session_id": "codex-thread", "source": source})
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(json.loads(result.stdout)["hookSpecificOutput"])
        self.assertTrue(all(output == outputs[0] for output in outputs))
        self.assertEqual(outputs[0]["hookEventName"], "SessionStart")
        self.assertIn("agent-comms send", outputs[0]["additionalContext"])
        records = list((self.paths.state / "sessions").glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())["harness"], "codex")

    def test_bad_input_and_missing_launcher_are_advisory(self):
        result = self.invoke({})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("setup failed", result.stderr)
        self.env["PATH"] = ""
        result = self.invoke({"session_id": "thread"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_manifest_and_hook_contract(self):
        manifest = json.loads((ROOT / "plugins/codex/.codex-plugin/plugin.json").read_text())
        config = json.loads((ROOT / "plugins/codex" / manifest["hooks"]).read_text())
        start = config["hooks"]["SessionStart"][0]
        self.assertIn("compact", start["matcher"])
        self.assertIn("fork", start["matcher"])
        self.assertIn("commandWindows", start["hooks"][0])
        self.assertNotIn("async", start["hooks"][0])


class InstallerTests(Fixture):
    def test_opencode_loader_is_idempotent_and_preserves_other_plugins(self):
        config = Path(self.temp.name) / "opencode"
        existing = config / "plugins/other/index.js"
        existing.parent.mkdir(parents=True)
        existing.write_text("user plugin")
        install_plugins.install_opencode(config)
        install_plugins.install_opencode(config)
        loader = (config / "plugins/agent-comms/index.js").read_text()
        self.assertIn((ROOT / "plugins/opencode/index.js").as_uri(), loader)
        self.assertEqual(existing.read_text(), "user plugin")

    def test_opencode_installer_does_not_overwrite_unfamiliar_plugin(self):
        config = Path(self.temp.name) / "opencode"
        target = config / "plugins/agent-comms/index.js"
        target.parent.mkdir(parents=True)
        target.write_text("user plugin")
        with self.assertRaises(RuntimeError):
            install_plugins.install_opencode(config)
        self.assertEqual(target.read_text(), "user plugin")

    def test_codex_installer_uses_official_cli_and_never_bypasses_trust(self):
        with patch.object(install_plugins, "codex", side_effect=[{"marketplaces": []}, {"ok": True}]) as invoke:
            with patch.object(install_plugins.subprocess, "run") as run:
                install_plugins.install_codex()
        self.assertEqual(run.call_args.args[0], ["codex", "plugin", "marketplace", "add", str(ROOT)])
        self.assertEqual(invoke.call_args.args, ("add", "agent-comms@agent-comms-local", "--json"))
