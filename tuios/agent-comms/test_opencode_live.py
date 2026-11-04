"""Opt-in real V2 dispatch against a local fake LLM; no external model calls."""
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import unittest

from test_agent_comms import Fixture, ROOT


@unittest.skipUnless(os.environ.get("AGENT_COMMS_TEST_OPENCODE") == "1" and shutil.which("opencode"), "opt-in OpenCode V2 integration")
class OpenCodeIntegrationTests(Fixture):
    @unittest.skipIf(os.name == "nt", "PTY startup test requires POSIX")
    def test_real_terminal_startup_names_pane_before_any_prompt(self):
        import fcntl
        import pty
        import struct
        import termios
        import sys

        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        title_log = Path(self.temp.name) / "title.json"
        tuios = bin_dir / "tuios"
        tuios.write_text(f'#!{sys.executable}\n' + f'''
import json,sys
pane={{"window_id":"terminal-startup","session":"test","workspace":1,"display_name":"OpenCode"}}
command=sys.argv[1]
if command=="session-info":
 data={{"tui_attached":True,"session_name":"test"}}
elif command=="list-windows":
 data={{"success":True,"panes":[pane],"total":1,"windows":[pane],"current_workspace":1,"focused_window_id":"terminal-startup"}}
elif command=="get-window":
 data={{"success":True,"id":"terminal-startup","title":"OpenCode","custom_name":""}}
else:
 open({str(title_log)!r},"w").write(json.dumps(sys.argv))
 data={{"success":True}}
print(json.dumps(data))
''')
        tuios.chmod(0o700)
        self.env["PATH"] = str(bin_dir) + os.pathsep + self.env["PATH"]
        self.start_server()  # Isolated test listener, never the installed service.
        env = {**self.env, "TERM": "xterm-256color", "OPENCODE_DB": ":memory:", "OPENCODE_LOG_LEVEL": "DEBUG",
               "COMMS_TEST_PROVIDER_KEY": "fake-local-key"}
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
            env[key] = str(Path(self.temp.name) / key)
            Path(env[key]).mkdir()
        env.update(TUIOS_ENV="1", TUIOS_PANE_ID="terminal-startup", TUIOS_SESSION="test",
                   TUIOS_SOCKET="/nonexistent/terminal-startup-test.sock")
        server, requests = self.fake_model()
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(self.model_config(server.server_port))
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        process = subprocess.Popen(["opencode", "--standalone"], cwd=self.temp.name, env=env,
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.addCleanup(self.cleanup_terminal, process)
        os.set_blocking(master, False)
        output = bytearray()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not title_log.exists():
            try:
                output.extend(os.read(master, 65536))
            except (BlockingIOError, OSError):
                pass
            if process.poll() is not None:
                break
            time.sleep(0.05)
        logs = "\n".join(p.read_text(errors="replace") for p in Path(self.temp.name).rglob("*.log"))
        errors = "\n".join(line for line in logs.splitlines() if "plugin" in line.lower() or "error" in line.lower())
        self.assertTrue(title_log.exists(), (errors + output.decode(errors="replace"))[-10000:])
        self.assertRegex(json.loads(title_log.read_text())[-1], r"^\[[A-Za-z]+[0-9]*\] OpenCode$")
        # Only now submit a prompt: the pane was named without any model call.
        self.assertFalse(requests)
        os.write(master, b"Say OK\r")
        deadline = time.monotonic() + 10
        system = ""
        while time.monotonic() < deadline:
            try:
                output.extend(os.read(master, 65536))
            except (BlockingIOError, OSError):
                pass
            system = "\n".join(str(message.get("content", "")) for body in requests
                               for message in body.get("messages", []) if message["role"] in ("system", "developer"))
            if "Checked local startup context" in system:
                break
            time.sleep(0.05)
        logs = "\n".join(p.read_text(errors="replace") for p in Path(self.temp.name).rglob("*.log"))
        errors = "\n".join(line for line in logs.splitlines() if any(word in line.lower() for word in ("plugin", "error", "rpc")))
        self.assertTrue("Checked local startup context" in system, errors[-6000:])
        self.assertIn('"epoch":', system)

    @staticmethod
    def cleanup_terminal(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def fake_model(self):
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
        return server, requests

    @staticmethod
    def model_config(port):
        return {
            "model": "comms-local/local-test",
            "plugins": [str(ROOT / "plugins/opencode")],
            "providers": {"comms-local": {
                "env": ["COMMS_TEST_PROVIDER_KEY"],
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                "models": {"local-test": {"limit": {"context": 32000, "output": 1000}}},
            }},
        }

    def test_real_dispatch_injects_comms_instructions_without_server_identity(self):
        server, requests = self.fake_model()
        env = {"PATH": os.environ["PATH"], "TERM": "dumb", "COMMS_TEST_PROVIDER_KEY": "fake-local-key", "OPENCODE_DB": ":memory:",
               "OPENCODE_LOG_LEVEL": "DEBUG"}
        for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
            env[key] = str(Path(self.temp.name) / key)
            Path(env[key]).mkdir()
        for key in ("AGENT_COMMS_CONFIG", "AGENT_COMMS_STATE_DIR", "AGENT_COMMS_RUNTIME_DIR"):
            env[key] = self.env[key]
        config = self.model_config(server.server_port)
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        result = subprocess.run(["opencode", "run", "--standalone", "--title", "Test", "Say OK"],
                                cwd=self.temp.name, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        system = "\n".join(str(message.get("content", "")) for body in requests
                           for message in body.get("messages", []) if message["role"] in ("system", "developer"))
        logs = "\n".join(p.read_text(errors="replace") for p in Path(self.temp.name).rglob("*.log"))
        diagnostic = "\n".join(line for line in logs.splitlines() if "plugin" in line.lower() or "error" in line.lower())
        self.assertIn("<agent-comms-session>", system, (result.stderr + diagnostic)[-10000:])
        self.assertIn("Close EVERY human-facing turn", system)
        self.assertIn("--name RETURNED_NAME --epoch RETURNED_EPOCH", system)
        self.assertNotIn("Personality:", system)
        self.assertIn("eleven_turbo_v2_5", system)
        self.assertIn("You are an AI agent running in OpenCode", system)
        self.assertFalse(self.paths.state.exists(), "instruction injection must not allocate identity")
