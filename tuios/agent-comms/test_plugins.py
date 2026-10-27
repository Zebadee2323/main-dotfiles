"""Identity stability and hook contracts; no real model, listener or trust changes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import install_plugins
import agent_comms as app
from test_agent_comms import Fixture, ROOT


class InstructionTests(Fixture):
    def configure_user(self, table):
        with self.paths.config.open("a", encoding="utf-8") as stream:
            stream.write("\n" + table + "\n")

    def test_user_names_render_in_both_output_formats_without_allocating_identity(self):
        self.configure_user('[user]\nname = "Oli"\ntts_name = "Ollie"')
        text = self.cli("instructions", "--format", "text")
        self.assertEqual(text.returncode, 0, text.stderr)
        instructions = json.loads(self.cli("instructions").stdout)["instructions"]
        self.assertEqual(text.stdout.strip(), instructions.strip())
        self.assertIn('"Oli" in written responses and `--msg`', instructions)
        self.assertIn('Use "Ollie" in `--tts`', instructions)
        self.assertIn("not agent identities", instructions)
        self.assertNotIn("{user_guidance}", instructions)
        self.assertFalse(self.paths.state.exists())

    def test_user_name_is_optional_and_spoken_name_defaults_to_written_name(self):
        instructions = json.loads(self.cli("instructions").stdout)["instructions"]
        self.assertIn("No user name is configured", instructions)
        self.assertNotIn("Ollie", instructions)
        self.configure_user('[user]\nname = "Zoë"')
        instructions = json.loads(self.cli("instructions").stdout)["instructions"]
        self.assertIn('"Zoë" in written responses', instructions)
        self.assertIn('Use "Zoë" in `--tts`', instructions)

    def test_user_names_are_quoted_and_not_recursively_interpolated(self):
        self.configure_user('[user]\nname = \'Zoë "Z" {speech_guidance}\'')
        instructions = json.loads(self.cli("instructions").stdout)["instructions"]
        self.assertIn(json.dumps('Zoë "Z" {speech_guidance}', ensure_ascii=False), instructions)

    def test_invalid_user_configuration_fails_clearly(self):
        original = self.paths.config.read_text()
        for table, error in (
            ('user = "Oli"', "user must be a table"),
            ('[user]\nname = 42', "user.name must be nonempty text"),
            ('[user]\nname = "  "', "user.name must be nonempty text"),
            ('[user]\nname = "Oli"\ntts_name = false', "user.tts_name must be nonempty text"),
            ('[user]\ntts_name = "Ollie"', "user.tts_name requires user.name"),
        ):
            with self.subTest(table=table):
                self.paths.config.write_text(table + "\n" + original)
                with self.assertRaisesRegex(app.CommsError, error):
                    app.load_config(self.paths.config)

    def test_opencode_plugin_injects_configured_user_names(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable")
        self.configure_user('[user]\nname = "Oli"\ntts_name = "Ollie"')
        script = f'''
import {{ createPlugin }} from {json.dumps((ROOT / "plugins/opencode/index.js").as_uri())};
let hook;
await createPlugin().setup({{ options: {{ python: {json.dumps(sys.executable)} }},
  session: {{ hook: async (_, fn) => {{ hook = fn; }} }} }});
const event = {{ sessionID: "ses_user_test", system: [] }};
await hook(event);
console.log(JSON.stringify(event.system));
'''
        result = subprocess.run([node, "--input-type=module"], input=script,
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        instructions = json.loads(result.stdout)[0]["text"]
        self.assertIn('"Oli" in written responses and `--msg`', instructions)
        self.assertIn('Use "Ollie" in `--tts`', instructions)
        self.assertFalse(self.paths.state.exists())

    def test_first_message_introduces_with_prefix_or_own_words_for_both_harnesses(self):
        for harness in ("opencode", "codex"):
            with self.subTest(harness=harness):
                result = self.cli("instructions")
                instructions = json.loads(result.stdout)["instructions"]
                self.assertIn("separate shell-tool call", instructions)
                self.assertIn("BEFORE composing", instructions)
                self.assertIn("combine it with send", instructions)
                self.assertIn("without another introduction", instructions)
                self.assertIn("No precautionary refresh", instructions)
                self.assertIn("--id RETURNED_ID", instructions)
                self.assertIn("or another pane/agent", instructions)
                self.assertIn("`agent-comms context`", instructions)
                self.assertNotIn("--harness", instructions)
                self.assertNotIn("--session-id", instructions)
                self.assertIn("identity, harness, or session ID", instructions)
                self.assertIn("Close EVERY human-facing turn", instructions)
                self.assertIn("immediately BEFORE", instructions)
                self.assertIn("eleven_turbo_v2_5", instructions)
                self.assertNotIn("Personality:", instructions)
                self.assertNotIn("Session identity:", instructions)
                self.assertFalse((self.paths.state / "bag.json").exists())

    def test_instructions_require_reuse_across_turns_and_compaction(self):
        instructions = json.loads(self.cli("instructions").stdout)["instructions"]
        self.assertIn("Reuse your latest successful same-pane `agent-comms context` result", instructions)
        self.assertIn("No precautionary refresh for new turns/tasks", instructions)
        self.assertIn("re-injected instructions", instructions)
        self.assertIn("after compaction or resume", instructions)
        self.assertIn("No result with known provenance", instructions)
        self.assertIn("without fresh context JSON", instructions)
        self.assertIn("again when fresh JSON was returned", instructions)
        self.assertIn("use the returned fresh context", instructions)
        self.assertNotIn("inherited/session/fork instructions", instructions)
        self.assertNotIn("You may reuse", instructions)

    def test_session_rendering_is_identical_and_never_uses_session_to_select_identity(self):
        outputs = [self.cli("instructions").stdout for _ in range(3)]
        self.assertTrue(all(output == outputs[0] for output in outputs))
        self.assertFalse(self.paths.state.exists())


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
        with self.paths.config.open("a", encoding="utf-8") as stream:
            stream.write('\n[user]\nname = "Oli"\ntts_name = "Ollie"\n')
        outputs = []
        for source in ("startup", "resume", "compact", "clear"):
            result = self.invoke({"session_id": "codex-thread", "source": source})
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(json.loads(result.stdout)["hookSpecificOutput"])
        self.assertTrue(all(output == outputs[0] for output in outputs))
        self.assertEqual(outputs[0]["hookEventName"], "SessionStart")
        self.assertIn("agent-comms send", outputs[0]["additionalContext"])
        self.assertIn("--id RETURNED_ID", outputs[0]["additionalContext"])
        self.assertIn('"Oli" in written responses and `--msg`', outputs[0]["additionalContext"])
        self.assertIn('Use "Ollie" in `--tts`', outputs[0]["additionalContext"])
        self.assertFalse(self.paths.state.exists(), "instruction hooks must not allocate identities")

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
