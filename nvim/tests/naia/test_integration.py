"""Direct HTTP MCP against the unmodified upstream server; no adapter process."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import pathlib
import runpy
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[2]
NVIM = os.environ.get("NVIM_BIN", "nvim")


class Client:
    """Minimal test-only Streamable HTTP client, like the native harness connection."""
    def __init__(self, info):
        self.url, self.token = info["url"], info["token"]
        self.session = None
        self.counter = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        initialized = self.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                  "clientInfo": {"name": "native-http-test", "version": "1"}})
        assert initialized["result"]["serverInfo"]["name"] == "mcp-tools-nvim"
        assert not initialized["result"]["capabilities"]["tools"].get("listChanged")
        self.request("notifications/initialized", notify=True)

    def request(self, method, params=None, notify=False):
        self.counter += 1
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            message["id"] = self.counter
        return self.send(message)

    def send(self, message, method="POST"):
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
            headers["MCP-Protocol-Version"] = "2025-03-26"
        data = None if message is None else json.dumps(message).encode()
        request = urllib.request.Request(self.url, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=15) as response:
            self.session = response.headers.get("Mcp-Session-Id", self.session)
            if method == "DELETE" or response.status in (202, 204):
                return None
            if response.headers.get_content_type() == "text/event-stream":
                data_lines = []
                for raw in response:
                    line = raw.decode().rstrip("\r\n")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    elif not line and data_lines:
                        item = json.loads("\n".join(data_lines))
                        data_lines = []
                        if item.get("id") == message.get("id"):
                            return item
                raise AssertionError("MCP stream ended without a reply")
            return json.load(response)

    def call(self, name, arguments=None):
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})["result"]

    def close(self):
        try:
            self.send(None, method="DELETE")
        except (OSError, urllib.error.HTTPError):
            pass


@unittest.skipUnless(os.environ.get("MCP_TOOLS_PATH") and os.environ.get("MCP_TOOLS_RUNTIME"),
                     "set MCP_TOOLS_PATH and MCP_TOOLS_RUNTIME to the independent owner guard")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.editors, self.clients = [], []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for client in self.clients:
            client.close()
        for process in self.editors:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process.log.close()

    def editor(self, marker):
        output = pathlib.Path(self.temp.name) / (marker + ".json")
        log = open(pathlib.Path(self.temp.name) / (marker + ".log"), "w+")
        env = {**os.environ, "NAIA_TEST_MARKER": marker, "NAIA_TEST_CONNECTION_OUT": str(output)}
        process = subprocess.Popen([NVIM, "--headless", "-u", "tests/naia/minimal_init.lua", "-l", "tests/naia/fixtures/live_editor.lua"],
                                   cwd=ROOT, env=env, stdout=log, stderr=log)
        process.log = log
        self.editors.append(process)
        deadline = time.monotonic() + 25
        while not output.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
        if not output.exists():
            log.seek(0)
            self.fail("Editor failed to start: " + log.read())
        return process, json.loads(output.read_text())

    def client(self, info):
        client = Client(info)
        self.clients.append(client)
        return client

    def remote(self, info, code):
        expr = "luaeval(" + json.dumps(code) + ")"
        return subprocess.run([NVIM, "--server", info["socket"], "--remote-expr", expr], capture_output=True, text=True, timeout=20, check=True)

    def test_native_http_tools_isolation_validation_and_lifecycle(self):
        first, a = self.editor("first")
        second, b = self.editor("second")
        one, two, other = self.client(a), self.client(a), self.client(b)
        self.assertTrue(one.session != two.session, "Native HTTP connections must use independent MCP sessions")
        self.assertEqual(one.call("nvim_echo", {"mode": "one"})["content"][0]["text"], "first:one")
        self.assertEqual(two.call("nvim_echo", {"mode": "two"})["content"][0]["text"], "first:two")
        self.assertEqual(other.call("nvim_echo", {"mode": "one"})["content"][0]["text"], "second:one")
        tools = {tool["name"]: tool for tool in one.request("tools/list")["result"]["tools"]}
        self.assertIn("nvim_third_party", tools)
        self.assertNotIn("nvim_naia_catalog", tools)
        self.assertNotIn("nvim_naia_dispatch", tools)
        self.assertIn("nvim_walkthrough_start", tools)
        self.assertFalse(any(name.startswith("nvim_ai_walkthrough_") for name in tools))
        self.assertEqual(tools["nvim_echo"]["inputSchema"]["required"], ["mode"])
        self.assertIn("one, two", tools["nvim_echo"]["inputSchema"]["properties"]["mode"]["description"])
        self.assertTrue(one.call("nvim_echo", {"mode": "invalid"})["isError"])
        self.assertTrue(one.call("nvim_echo")["isError"])
        self.assertEqual(json.loads(one.call("nvim_object")["content"][0]["text"]), {"status": "ok", "marker": "first"})
        self.assertTrue(one.call("nvim_fail")["isError"])
        self.assertEqual(one.call("nvim_third_party")["content"][0]["text"], "independent provider")
        self.assertEqual(one.call("nvim_nvim_already_prefixed")["content"][0]["text"], "unchanged")
        # Model-visible nested shapes survive the upstream argument format.
        self.assertIn("properties", tools["nvim_walkthrough_start"]["inputSchema"]["properties"]["steps"]["items"])
        pending = one.call("nvim_walkthrough_start", {"steps": [{"path": str(ROOT / "README.md"),
                             "line_start": 1, "line_end": 1, "description": "Headless confirmation test"}]})
        self.assertFalse(pending.get("isError"), pending["content"][0]["text"])
        self.assertEqual(json.loads(pending["content"][0]["text"])["status"], "awaiting_confirmation")
        # Independent concurrent HTTP requests keep the upstream correlation IDs.
        with ThreadPoolExecutor(max_workers=2) as pool:
            slow = pool.submit(one.call, "nvim_slow")
            quick = pool.submit(two.call, "nvim_echo", {"mode": "two"})
            self.assertEqual(slow.result()["content"][0]["text"], "slow result")
            self.assertEqual(quick.result()["content"][0]["text"], "first:two")
        one.call("nvim_add_tool")
        reconnected = self.client(a)
        self.assertIn("nvim_dynamic", {tool["name"] for tool in reconnected.request("tools/list")["result"]["tools"]})
        self.assertEqual(reconnected.call("nvim_dynamic")["content"][0]["text"], "dynamic result")
        one.call("nvim_remove_tool")
        self.assertTrue(two.call("nvim_dynamic")["isError"])

        self.remote(a, "require('naia').stop()")
        self.assertIsNone(first.poll())
        self.assertEqual(one.call("nvim_third_party")["content"][0]["text"], "independent provider")
        self.assertTrue(one.call("nvim_echo", {"mode": "one"})["isError"])
        self.remote(a, "require('naia').start()")
        self.assertEqual(one.call("nvim_echo", {"mode": "one"})["content"][0]["text"], "first:one")
        self.remote(a, "require('naia').restart()")
        launcher = runpy.run_path(str(ROOT / "bin" / "naia-attach"), run_name="integration_launcher")
        with patch.dict(os.environ, {"NVIM": a["socket"]}):
            self.assertTrue(launcher["get_naia_connection_info"]() == {"url": a["url"], "token": a["token"]},
                            "Naia registration restart must not change bridge credentials")
        self.remote(a, "require('mcp-tools').stop()")
        self.assertIsNone(first.poll(), "upstream stop must not quit Neovim")
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(a["url"] + "health", timeout=2)
        with patch.dict(os.environ, {"NVIM": a["socket"]}):
            self.assertIsNone(launcher["get_naia_connection_info"](), "the launcher must not start transport")
        self.remote(a, "(function() require('mcp-tools').start(); return vim.wait(15000, function() return require('mcp-tools').get_port() ~= nil end, 20) end)()")
        with patch.dict(os.environ, {"NVIM": a["socket"]}):
            new = launcher["get_naia_connection_info"]()
        self.assertTrue(new["token"] != a["token"], "Externally owned restart must rotate authentication")
        fresh = self.client(new)
        self.assertEqual(fresh.call("nvim_echo", {"mode": "one"})["content"][0]["text"], "first:one")
        self.assertEqual(other.call("nvim_echo", {"mode": "two"})["content"][0]["text"], "second:two")
        self.assertIsNone(second.poll())


if __name__ == "__main__":
    unittest.main()
