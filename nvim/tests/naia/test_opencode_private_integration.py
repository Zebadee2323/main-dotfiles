#!/usr/bin/env python3
"""Opt-in real OpenCode test, with an isolated home/database and a local fake LLM.

Run: NAIA_TEST_OPENCODE=1 python3 tests/naia/test_opencode_private_integration.py
No real model, editor, shared service, or user configuration is used.
"""

import http.server
import json
import os
import pathlib
import runpy
import shutil
import subprocess
import tempfile
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE = runpy.run_path(str(ROOT / "bin" / "sidekick-naia-cli"))


@unittest.skipUnless(os.environ.get("NAIA_TEST_OPENCODE") == "1" and shutil.which("opencode"), "opt-in OpenCode integration")
class PrivateServerIntegrationTests(unittest.TestCase):
    def test_new_sessions_receive_rules_but_ordinary_launch_does_not(self):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                self.send_response(200)
                if body.get("stream"):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for delta, finish in (({"role": "assistant", "content": "OK"}, None), ({}, "stop")):
                        chunk = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1,
                                 "model": "naia-test", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"id": "chatcmpl-test", "object": "chat.completion", "created": 1,
                                                "model": "naia-test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Test"}, "finish_reason": "stop"}],
                                                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode())

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="naia-private-test-", dir=os.environ.get("NAIA_TEST_TMP")) as root:
            env = {"PATH": os.environ["PATH"], "TERM": "dumb", "NAIA_TEST_PROVIDER_KEY": "fake-local-key", "OPENCODE_DB": ":memory:"}
            for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
                env[key] = str(pathlib.Path(root) / key)
                pathlib.Path(env[key]).mkdir()
            base = {
                "model": "naia-local/naia-test",
                "providers": {"naia-local": {
                    "env": ["NAIA_TEST_PROVIDER_KEY"],
                    "package": "@opencode/ai/providers/openai-compatible",
                    "settings": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1"},
                    "models": {"naia-test": {"limit": {"context": 32000, "output": 1000}}},
                }},
            }
            for use_naia in (True, True, False):
                config = {**base, "plugins": [MODULE["OPENCODE_PLUGIN_DIR"]] if use_naia else []}
                env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
                start = len(requests)
                result = subprocess.run(["opencode", "run", "--standalone", "--title", "Test", "Say OK"],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr[-3000:])
                actual = requests[start:]
                self.assertTrue(actual, "expected a request to the local fake model")
                system = "\n".join(str(message.get("content", "")) for request in actual
                                   for message in request.get("messages", []) if message["role"] in ("system", "developer"))
                self.assertEqual("# Showing things in Neovim" in system, use_naia, system[:500])
                self.assertNotIn("nvim_inform_user", system)


if __name__ == "__main__":
    unittest.main()
