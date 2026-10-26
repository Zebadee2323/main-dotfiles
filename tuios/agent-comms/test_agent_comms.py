"""Standard-library tests. All files, ports, and processes are isolated."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import agent_comms as app

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "agent_comms.py"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-comms-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = app.Paths(root / "config/config.toml", root / "state", root / "run")
        self.paths.config.parent.mkdir()
        self.paths.config.write_text(
            'display_seconds = 0.18\n'
            '[identities.a]\nname = "Alice"\neleven_labs_voice_id = ""\npersonality = ""\n'
            '[identities.b]\nname = "Bob"\neleven_labs_voice_id = ""\npersonality = ""\n'
            '[identities.c]\nname = "Cleo"\neleven_labs_voice_id = ""\npersonality = ""\n',
            encoding="utf-8",
        )
        self.env = {
            # Test sends must not inspect the user's live tuios focus or inherit
            # a paid speech credential. Context/speech tests supply their own.
            **{key: value for key, value in os.environ.items()
               if not key.startswith(("TUIOS_", "ELEVENLABS_"))},
            "AGENT_COMMS_CONFIG": str(self.paths.config),
            "AGENT_COMMS_STATE_DIR": str(self.paths.state),
            "AGENT_COMMS_RUNTIME_DIR": str(self.paths.runtime),
            "PYTHONUNBUFFERED": "1",
        }

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True, encoding="utf-8", timeout=10)

    def process(self, *args):
        process = subprocess.Popen([sys.executable, str(SCRIPT), *args], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8")
        self.addCleanup(self.cleanup_process, process)
        return process

    @staticmethod
    def cleanup_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()

    def start_server(self):
        process = self.process("serve", "--quiet")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(process.stderr.read())
            try:
                info = app.endpoint(self.paths)
                # A stale file may still exist after a crashed previous server.
                with socket.create_connection(("127.0.0.1", info["port"]), timeout=0.1) as conn:
                    conn.sendall(json.dumps({"command": "watch", "protocol_version": app.VERSION, "token": info["token"]}).encode() + b"\n")
                    with conn.makefile("rb") as stream:
                        self.assertTrue(json.loads(stream.readline())["ok"])
                return process, info
            except (app.CommsError, OSError):
                time.sleep(0.01)
        self.fail("Server did not publish discovery data")

    def request(self, command, **kwargs):
        info = app.endpoint(self.paths)
        connection = socket.create_connection(("127.0.0.1", info["port"]), timeout=2)
        stream = connection.makefile("rwb")
        self.addCleanup(stream.close)
        self.addCleanup(connection.close)
        stream.write(json.dumps({"protocol_version": app.VERSION, "token": info["token"],
                                 "command": command, **kwargs}).encode() + b"\n")
        stream.flush()
        return connection, stream

    def subscribe(self):
        _, stream = self.request("watch")
        self.assertTrue(json.loads(stream.readline())["ok"])
        return stream

    def message(self, stream):
        while True:
            message = json.loads(stream.readline())["message"]
            if message is None or message.get("phase") == "displaying":
                return message


class BagTests(Fixture):
    def test_two_cycles_and_refill_only(self):
        for _ in range(2):
            draws = [self.cli("id") for _ in range(3)]
            self.assertTrue(all(p.returncode == 0 for p in draws))
            self.assertEqual(sorted(p.stdout.strip() for p in draws), ["a", "b", "c"])
        self.assertEqual(self.cli("id", "--refill").stdout, "")
        state = json.loads((self.paths.state / "bag.json").read_text())
        self.assertEqual(len(state["remaining"]), 3)

    def test_concurrent_draws_preserve_cycle_accounting(self):
        self.cli("id", "--refill")
        with ThreadPoolExecutor(max_workers=9) as pool:
            draws = list(pool.map(lambda _: self.cli("id"), range(30)))
        self.assertTrue(all(p.returncode == 0 for p in draws), [p.stderr for p in draws])
        for identity in ("a", "b", "c"):
            self.assertEqual(sum(p.stdout.strip() == identity for p in draws), 10)
        self.assertEqual(json.loads((self.paths.state / "bag.json").read_text())["remaining"], [])

    def test_front_of_persisted_bag_is_drawn(self):
        self.paths.state.mkdir()
        app.atomic_json(self.paths.state / "bag.json", {"ids": ["a", "b", "c"], "remaining": ["c", "a"]})
        self.assertEqual(self.cli("id").stdout, "c\n")
        self.assertEqual(self.cli("id").stdout, "a\n")

    def test_config_change_and_corrupt_state_start_new_cycle(self):
        self.cli("id")
        state = self.paths.state / "bag.json"
        state.write_text("broken json")
        result = self.cli("id")
        self.assertEqual(result.returncode, 0)
        self.assertIn("corrupt", result.stderr)
        with self.paths.config.open("a") as stream:
            stream.write('[identities.d]\nname = "Delta"\n')
        self.cli("id")
        self.assertEqual(json.loads(state.read_text())["ids"], ["a", "b", "c", "d"])

    def test_missing_config_is_actionable(self):
        self.paths.config.unlink()
        result = self.cli("id")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot read identity config", result.stderr)
        self.assertFalse(self.paths.config.exists())

    def test_invalid_config_and_global_overrides(self):
        result = self.cli("--runtime-dir", str(self.paths.runtime), "id", "--state-dir", str(self.paths.state))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.paths.config.write_text('display_seconds = nan\n[identities.x]\nname = "X"\n')
        result = self.cli("id")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("display_seconds", result.stderr)


class ServerTests(Fixture):
    def test_offline_send_does_not_start_server(self):
        result = self.cli("send", "--id", "a", "--msg", "hello")
        self.assertEqual(result.returncode, 1)
        self.assertIn("offline", result.stderr)
        self.assertFalse((self.paths.runtime / "server.json").exists())

    def test_configured_expiration_and_server_sanitization(self):
        self.start_server()
        stream = self.subscribe()
        _, sender = self.request("send", id="a", msg="hello\nworld\x1b[31m!\x1b[0m")
        self.assertTrue(json.loads(sender.readline())["ok"])
        start = time.monotonic()
        active = self.message(stream)
        self.assertEqual({key: active[key] for key in ("id", "name", "msg")},
                         {"id": "a", "name": "Alice", "msg": "hello\nworld!"})
        self.assertIsNone(self.message(stream))
        elapsed = time.monotonic() - start
        self.assertGreater(elapsed, 0.12)
        self.assertLess(elapsed, 2)

    def test_invalid_discovery_is_actionable(self):
        self.paths.runtime.mkdir()
        discovery = self.paths.runtime / "server.json"
        for content, expected in (("broken", "Invalid discovery"),
                                  ('{"protocol_version": 99}', "Unsupported discovery")):
            discovery.write_text(content)
            result = self.cli("send", "--id", "a", "--msg", "hello")
            self.assertEqual(result.returncode, 1)
            self.assertIn(expected, result.stderr)

    def test_singleton_and_permissions_and_shutdown(self):
        server, info = self.start_server()
        self.assertTrue(1 <= info["port"] <= 65535)
        self.assertEqual(len(info["token"]), 64)
        second = self.cli("serve", "--quiet")
        self.assertEqual(second.returncode, 1)
        self.assertIn("already running", second.stderr)
        self.assertEqual(app.endpoint(self.paths), info)
        if os.name != "nt":
            self.assertEqual(self.paths.runtime.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.paths.runtime / "server.json").stat().st_mode & 0o777, 0o600)
            server.terminate()
            self.assertEqual(server.wait(timeout=5), 0)
            self.assertFalse((self.paths.runtime / "server.json").exists())

    def test_fifo_multiple_watchers_and_initial_snapshot(self):
        self.start_server()
        first, second = self.subscribe(), self.subscribe()
        for identity, text in (("a", "first"), ("b", "second"), ("c", "third")):
            _, stream = self.request("send", id=identity, msg=text)
            self.assertTrue(json.loads(stream.readline())["ok"])
        for text in ("first", "second", "third"):
            one, two = self.message(first), self.message(second)
            self.assertEqual(one, two)
            self.assertEqual(one["msg"], text)
        self.assertIsNone(self.message(first))
        self.assertIsNone(self.message(second))
        # A newly attached watcher receives an active message immediately.
        self.assertEqual(self.cli("send", "--id", "a", "--msg", "current").returncode, 0)
        _, stream = self.request("watch")
        self.assertEqual(json.loads(stream.readline())["message"]["msg"], "current")

    def test_authentication_protocol_and_validation(self):
        self.start_server()
        for payload, error in (({"token": "bad"}, "Authentication"),
                               ({"protocol_version": 999}, "protocol"),
                               ({"id": "unknown"}, "Unknown identity"),
                               ({"msg": " "}, "nonempty"),
                               ({"msg": "x" * (app.MAX_MESSAGE + 1)}, "exceeds"),
                               ({"msg": "\ud800"}, "Unicode"),
                               ({"token": "\ud800"}, "Authentication")):
            _, stream = self.request("send", **{"id": "a", "msg": "hi", **payload})
            reply = json.loads(stream.readline())
            self.assertFalse(reply["ok"])
            self.assertIn(error, reply["error"])
        self.assertEqual(self.cli("send", "--id", "a", "--msg", "healthy").returncode, 0)

    def test_malformed_and_oversized_frames_do_not_break_server(self):
        _, info = self.start_server()
        for frame in (b"[]\n", b"not-json\n", b"x" * (app.MAX_FRAME + 1) + b"\n"):
            with socket.create_connection(("127.0.0.1", info["port"]), timeout=2) as connection:
                connection.sendall(frame)
                reply = connection.recv(4096)
                self.assertIn(b'"ok": false', reply)
        self.assertEqual(self.cli("send", "--id", "a", "--msg", "still alive").returncode, 0)

    def test_crash_recovery_replaces_stale_discovery(self):
        server, old = self.start_server()
        server.kill()
        server.wait(timeout=5)
        self.assertTrue((self.paths.runtime / "server.json").exists())
        _, new = self.start_server()
        self.assertNotEqual(old["token"], new["token"])
        self.assertEqual(self.cli("send", "--id", "b", "--msg", "recovered").returncode, 0)

    def test_watcher_reconnects_across_offline_and_restart(self):
        watcher = self.process("watch", "--no-color")
        lines = queue.Queue()

        def read_lines():
            for line in watcher.stdout:
                lines.put(line.strip())

        thread = threading.Thread(target=read_lines, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)

        def until(expected):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                line = lines.get(timeout=max(0.01, deadline - time.monotonic()))
                if expected in line:
                    return line
            self.fail(f"Watcher did not show {expected}")

        until("offline")
        server, _ = self.start_server()
        until("idle")
        self.cli("send", "--id", "a", "--msg", "watch this")
        self.assertIn("Alice: watch this", until("watch this"))
        server.kill()
        server.wait(timeout=5)
        until("offline")
        self.start_server()
        until("idle")
        self.assertIsNone(watcher.poll())
        self.cleanup_process(watcher)
        thread.join(timeout=2)


class RenderingTests(unittest.TestCase):
    def test_safe_single_line_and_color(self):
        message = {"id": "a", "name": "Alice", "msg": "hello\nworld\x1b[31mred\x1b[0m\x1b]52;c;bad\x07\u202e"}
        plain = app.dock_line(message, color=False)
        self.assertNotIn("\n", plain)
        self.assertNotIn("\x1b", plain)
        self.assertNotIn("bad", plain)
        self.assertNotIn("\u202e", plain)
        self.assertIn("Alice: hello worldred", plain)
        self.assertIn("\x1b[", app.dock_line(message))

    def test_width_and_idle(self):
        text = app.dock_line({"id": "x", "name": "Name", "msg": "界" * 100}, width=30, color=False)
        self.assertLessEqual(app.cell_width(text), 30)
        self.assertTrue(text.endswith("..."))
        self.assertIn("idle", app.dock_line(None))
        self.assertIn("offline", app.dock_line(None, offline=True))

    def test_windows_paths(self):
        args = app.parser().parse_args(["id"])
        # Select Windows defaults without changing pathlib's host platform.
        root = Path.home() / "mock-appdata"
        fake_os = SimpleNamespace(name="nt", environ={
            "LOCALAPPDATA": str(root / "local"), "APPDATA": str(root / "roaming"),
        })
        with patch.object(app, "os", fake_os):
            paths = app.Paths.resolve(args)
        self.assertEqual(paths.runtime, root / "local/agent-comms/run")
        self.assertEqual(paths.config, ROOT / "config.toml")

    def test_default_config_is_relative_to_program_not_cwd(self):
        args = app.parser().parse_args(["id"])
        with patch.dict(os.environ, {}, clear=True):
            paths = app.Paths.resolve(args)
        self.assertEqual(paths.config, ROOT / "config.toml")
        self.assertTrue(app.load_config(paths.config)["identities"], "the default roster is user-editable")
        self.assertFalse((ROOT / "config.lock").exists())


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_denied_is_not_reported_as_listener_offline(self):
        paths = app.Paths(Path("unused"), Path("unused"), Path("unused"))
        with patch.object(app, "endpoint", return_value={"port": 12345, "token": "0" * 64}):
            with patch.object(app.asyncio, "open_connection", new=AsyncMock(side_effect=PermissionError(1, "Operation not permitted"))):
                with self.assertRaises(app.CommsError) as error:
                    await app.connect(paths, "send")
        self.assertIn("blocked by permissions or a sandbox", str(error.exception))
        self.assertNotIn("offline", str(error.exception))

    async def test_full_queue_rejects_without_dropping_accepted_message(self):
        config = {"identities": {"a": {"name": "Alice"}}}
        temp = tempfile.TemporaryDirectory(prefix="agent-comms-queue-test-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        server = app.Server(app.Paths(root / "config.toml", root / "state", root / "run"), config, quiet=True)
        server.queue = asyncio.Queue(maxsize=1)
        listener = await asyncio.start_server(server.handle, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]

        async def send(text):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await app.write_frame(writer, {"protocol_version": app.VERSION, "token": server.token,
                                               "command": "send", "id": "a", "msg": text})
                return await app.read_frame(reader)
            finally:
                await app.close_writer(writer)

        try:
            self.assertTrue((await send("first"))["ok"])
            second = await send("second")
            self.assertFalse(second["ok"])
            self.assertIn("full", second["error"])
            self.assertEqual(server.queue.get_nowait()["msg"], "first")
        finally:
            listener.close()
            await listener.wait_closed()
            await asyncio.gather(*list(server.tasks), return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
