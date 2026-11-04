"""Opt-in real V2 dispatch against a local fake LLM; no external model calls."""
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import unittest

from test_agent_comms import Fixture, ROOT


@unittest.skipUnless(os.environ.get("AGENT_COMMS_TEST_OPENCODE") == "1" and shutil.which("opencode"), "opt-in OpenCode V2 integration")
class OpenCodeIntegrationTests(Fixture):
    def test_real_dispatch_injects_identity_alongside_neovim_rules(self):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
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
                                 "model": "local-test", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"id": "chatcmpl-test", "object": "chat.completion", "created": 1,
                                                "model": "local-test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Test"}, "finish_reason": "stop"}],
                                                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode())

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = {"PATH": os.environ["PATH"], "TERM": "dumb", "COMMS_TEST_PROVIDER_KEY": "fake-local-key", "OPENCODE_DB": ":memory:"}
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
            env[key] = str(Path(self.temp.name) / key)
            Path(env[key]).mkdir()
        for key in ("AGENT_COMMS_CONFIG", "AGENT_COMMS_STATE_DIR", "AGENT_COMMS_RUNTIME_DIR"):
            env[key] = self.env[key]
        config = {
            "model": "comms-local/local-test",
            "plugins": [str(ROOT / "plugins/opencode"), str(ROOT.parents[1] / "nvim/opencode/naia")],
            "providers": {"comms-local": {
                "env": ["COMMS_TEST_PROVIDER_KEY"],
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1"},
                "models": {"local-test": {"limit": {"context": 32000, "output": 1000}}},
            }},
        }
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        result = subprocess.run(["opencode", "run", "--standalone", "--title", "Test", "Say OK"],
                                cwd=self.temp.name, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        system = "\n".join(str(message.get("content", "")) for body in requests
                           for message in body.get("messages", []) if message["role"] in ("system", "developer"))
        self.assertIn("<agent-comms-session>", system)
        self.assertIn("Always close every turn", system)
        self.assertIn("--id RETURNED_ID", system)
        self.assertNotIn("Personality:", system)
        self.assertIn("eleven_turbo_v2_5", system)
        self.assertIn("# Showing things in Neovim", system)
        self.assertNotIn("with `nvim_inform_user`", system)
        self.assertFalse(self.paths.state.exists(), "instruction injection must not allocate identity")
