#!/usr/bin/env python3
"""Opt-in native skill discovery, with isolated HOME and no model requests."""
import json
import os
from pathlib import Path
import queue
import runpy
import shutil
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[2]
APP = runpy.run_path(str(ROOT / "bin/naia-attach"))


@unittest.skipUnless(os.environ.get("NAIA_TEST_CODEX") == "1" and shutil.which("codex"),
                     "opt-in Codex skill integration")
class CodexSkillIntegrationTests(unittest.TestCase):
    def test_codex_discovers_the_real_skill_through_the_installed_link(self):
        with tempfile.TemporaryDirectory(prefix="naia-codex-test-", dir=os.environ.get("NAIA_TEST_TMP")) as root:
            env = {"PATH": os.environ["PATH"], "HOME": root, "CODEX_HOME": root + "/codex"}
            APP["ensure_codex_skill"](env)
            with tempfile.TemporaryFile(mode="w+") as errors:
                process = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=errors, text=True,
                                           env=env, cwd=root)
                messages = queue.Queue()

                def read():
                    for line in process.stdout:
                        messages.put(json.loads(line))

                reader = threading.Thread(target=read, daemon=True)
                reader.start()

                def request(identifier, method, params):
                    process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
                    process.stdin.flush()
                    while True:
                        message = messages.get(timeout=30)
                        if message.get("id") == identifier:
                            self.assertNotIn("error", message)
                            return message["result"]

                try:
                    request(1, "initialize", {"clientInfo": {"name": "naia-skill-test", "version": "1.0"}})
                    process.stdin.write('{"method":"initialized"}\n')
                    process.stdin.flush()
                    result = request(2, "skills/list", {"cwds": [root], "forceReload": True})
                    skills = [skill for item in result["data"] for skill in item["skills"]
                              if Path(skill["path"]).resolve() == APP["NAIA_SKILL"] / "SKILL.md"]
                    self.assertEqual(len(skills), 1, result)
                    self.assertTrue(skills[0]["enabled"])
                    # Codex detects the shared Claude plugin manifest at the real path.
                    self.assertEqual(skills[0]["name"], "naia:naia")
                finally:
                    process.terminate()
                    process.wait(timeout=10)
                    reader.join(timeout=5)
                    process.stdin.close()
                    process.stdout.close()


if __name__ == "__main__":
    unittest.main()
