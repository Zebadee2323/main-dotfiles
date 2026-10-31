"""Standard-library tests. All files, ports, and processes are isolated."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import queue
import re
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
import allocation
import pane_identity

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "agent_comms.py"
# A fake pane on a nonexistent tuios socket: tests never reach the user's live daemon.
TEST_PANE = {"TUIOS_ENV": "1", "TUIOS_SESSION": "test", "TUIOS_SOCKET": "/nonexistent/agent-comms-test.sock"}
NAMES = 'female = ["Alice", "Cleo"]\nmale = ["Bob"]\n'
VOICES = ('[voice_groups.main.voices.voice_f]\ngender = "female"\neleven_labs_voice_id = ""\n'
          '[voice_groups.main.voices.voice_m]\ngender = "male"\neleven_labs_voice_id = ""\n')


def write_config(config: Path, body="", *, names=NAMES, voices=VOICES):
    """Write a config plus its names.toml. body precedes the voice tables (top-level keys)."""
    config.parent.mkdir(parents=True, exist_ok=True)
    (config.parent / "names.toml").write_text(names, encoding="utf-8")
    config.write_text('names_file = "names.toml"\n' + body + voices, encoding="utf-8")


def seed_binding(paths, env, name, *, profile=None, generation=0):
    """Deterministically bind an owner to a catalog name; returns the epoch."""
    config = app.load_config(paths.config)
    key, daemon, pane = pane_identity.owner(env)
    store = pane_identity.store(paths, lock=app.FileLock, atomic_json=app.atomic_json, private_dir=app.private_dir)
    base = re.match(r"[A-Za-z]+", name).group(0)

    def operation(state):
        entry = next((e for e in state["names"]["remaining"] if e[0] == base), None)
        gender = entry[1] if entry else dict(config["names"])[base]
        if entry and generation == state["names"]["generation"]:
            state["names"]["remaining"].remove(entry)
        chosen = profile or allocation.eligible_profiles(config)[gender][0]
        state["bindings"][key] = {
            "daemon": daemon, "pane_id": pane, "name": name, "base_name": base, "gender": gender,
            "name_generation": generation, "voice_profile_id": chosen, "voice_group": config["voice_group_of"][chosen],
            "voice_profile": allocation.snapshot(config, chosen), "assigned_at": allocation.now_iso()}
        state["names"]["generation"] = max(state["names"]["generation"], generation)
        state["scanned"][daemon] = time.time()  # Never inventory the live tuios daemon from tests.
        return state["epoch"]
    return store.update(operation, config=config)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agent-comms-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = app.Paths(root / "config/config.toml", root / "state", root / "run")
        write_config(self.paths.config, 'display_seconds = 0.18\n')
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

    def pane_env(self, pane):
        return {**TEST_PANE, "TUIOS_PANE_ID": pane}

    def sender(self, name="Alice", pane=None):
        """Bound send fields (name, epoch, pane_env) for a fake pane, seeded once per name."""
        bound = self.__dict__.setdefault("_bound", {})
        if name not in bound:
            env = self.pane_env(pane or f"pane-{name.lower()}")
            bound[name] = {"name": name, "epoch": seed_binding(self.paths, env, name), "pane_env": env}
        return dict(bound[name])

    def transport_send(self, name, message, tts=None):
        """Exercise transport in isolation; pane attribution has separate CLI tests."""
        sender = self.sender(name)
        code = ("import asyncio,sys; import agent_comms as a; "
                "p=a.Paths.resolve(a.parser().parse_args(['serve'])); "
                "name,epoch,message,tts=sys.argv[1:];\n"
                "try:\n"
                " a.validate_message(a.load_config(p.config),name,message,tts)\n"
                " asyncio.run(a.send(p,name,epoch,message,tts))\n"
                "except (a.CommsError,OSError) as e:\n"
                " print(str(e),file=sys.stderr); sys.exit(1)\n")
        return subprocess.run([sys.executable, "-c", code, name, sender["epoch"], message,
                               message if tts is None else tts],
                              cwd=ROOT, env={**self.env, **sender["pane_env"]}, capture_output=True, text=True, timeout=10)

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
            frame = json.loads(stream.readline())
            message = frame["message"]
            if message is not None and frame.get("lifecycle", {}).get("event") == "end":
                continue  # The ended message stays current only until the next start.
            if message is None or message.get("phase") == "displaying":
                return message


class ConfigTests(Fixture):
    def test_missing_config_is_actionable(self):
        self.paths.config.unlink()
        result = self.cli("context")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot read agent-comms config", result.stderr)
        self.assertFalse(self.paths.config.exists())

    def test_invalid_config_and_global_overrides(self):
        result = self.cli("--runtime-dir", str(self.paths.runtime), "bags", "status", "--state-dir", str(self.paths.state))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"initialized": False})
        write_config(self.paths.config, "display_seconds = nan\n")
        result = self.cli("context")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("display_seconds", result.stderr)


class ServerTests(Fixture):
    def test_offline_send_does_not_start_server(self):
        result = self.transport_send("Alice", "hello")
        self.assertEqual(result.returncode, 1)
        self.assertIn("offline", result.stderr)
        self.assertFalse((self.paths.runtime / "server.json").exists())

    def test_configured_expiration_and_server_sanitization(self):
        self.start_server()
        stream = self.subscribe()
        _, sender = self.request("send", **self.sender("Alice"), msg="hello\nworld\x1b[31m!\x1b[0m")
        self.assertTrue(json.loads(sender.readline())["ok"])
        start = time.monotonic()
        active = self.message(stream)
        self.assertEqual({key: active[key] for key in ("id", "name", "msg")},
                         {"id": "Alice", "name": "Alice", "msg": "hello\nworld!"})
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
            result = self.transport_send("Alice", "hello")
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

    @unittest.skipIf(os.name == "nt", "Requires POSIX termination signals")
    def test_shutdown_disconnects_watchers_and_releases_singleton(self):
        server, _ = self.start_server()
        watchers = [self.subscribe(), self.subscribe()]
        server.terminate()
        self.assertEqual(server.wait(timeout=5), 0)
        for stream in watchers:
            self.assertEqual(stream.readline(), b"")
        self.assertFalse((self.paths.runtime / "server.json").exists())
        # A clean shutdown must also release the lock for the next listener.
        replacement, _ = self.start_server()
        replacement.terminate()
        self.assertEqual(replacement.wait(timeout=5), 0)

    def test_fifo_multiple_watchers_and_initial_snapshot(self):
        self.start_server()
        first, second = self.subscribe(), self.subscribe()
        for identity, text in (("Alice", "first"), ("Bob", "second"), ("Cleo", "third")):
            _, stream = self.request("send", **self.sender(identity), msg=text)
            self.assertTrue(json.loads(stream.readline())["ok"])
        for text in ("first", "second", "third"):
            one, two = self.message(first), self.message(second)
            self.assertEqual(one, two)
            self.assertEqual(one["msg"], text)
        self.assertIsNone(self.message(first))
        self.assertIsNone(self.message(second))
        # A newly attached watcher receives an active message immediately.
        self.assertEqual(self.transport_send("Alice", "current").returncode, 0)
        _, stream = self.request("watch")
        self.assertEqual(json.loads(stream.readline())["message"]["msg"], "current")

    def test_authentication_protocol_and_validation(self):
        self.start_server()
        for payload, error in (({"token": "bad"}, "Authentication"),
                               ({"protocol_version": 999}, "protocol"),
                               ({"name": "Unknown"}, "mismatch"),
                               ({"epoch": "0" * 16}, "mismatch"),
                               ({"pane_env": TEST_PANE | {"TUIOS_PANE_ID": "unbound"}}, "no current agent name"),
                               ({"msg": " "}, "nonempty"),
                               ({"msg": "x" * (app.MAX_MESSAGE + 1)}, "exceeds"),
                               ({"msg": "\ud800"}, "Unicode"),
                               ({"token": "\ud800"}, "Authentication")):
            _, stream = self.request("send", **{**self.sender("Alice"), "msg": "hi", **payload})
            reply = json.loads(stream.readline())
            self.assertFalse(reply["ok"])
            self.assertIn(error, reply["error"])
        self.assertEqual(self.transport_send("Alice", "healthy").returncode, 0)

    def test_malformed_and_oversized_frames_do_not_break_server(self):
        _, info = self.start_server()
        for frame in (b"[]\n", b"not-json\n", b"x" * (app.MAX_FRAME + 1) + b"\n"):
            with socket.create_connection(("127.0.0.1", info["port"]), timeout=2) as connection:
                connection.sendall(frame)
                reply = connection.recv(4096)
                self.assertIn(b'"ok": false', reply)
        self.assertEqual(self.transport_send("Alice", "still alive").returncode, 0)

    def test_crash_recovery_replaces_stale_discovery(self):
        server, old = self.start_server()
        server.kill()
        server.wait(timeout=5)
        self.assertTrue((self.paths.runtime / "server.json").exists())
        _, new = self.start_server()
        self.assertNotEqual(old["token"], new["token"])
        self.assertEqual(self.transport_send("Bob", "recovered").returncode, 0)

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
        self.transport_send("Alice", "watch this")
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
    def test_loaded_tts_mode_badges(self):
        message = {"id": "a", "name": "Alice", "msg": "hello"}
        for mode, badge in (("elevenlabs", "[ELE]"), ("phonon", "[PHO]"), ("pocket_tts", "[POC]")):
            for value in (None, message):
                with self.subTest(mode=mode, message=value):
                    self.assertTrue(app.dock_line(value, color=False, mode=mode).startswith(badge))
                    self.assertIn(badge, app.dock_line(value, mode=mode))
                    self.assertIn(badge, app.dock_line(value, mode=mode, highlight=True))
        self.assertTrue(app.dock_line(None, color=False).startswith("[ELE]"))

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
        args = app.parser().parse_args(["context"])
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
        args = app.parser().parse_args(["context"])
        with patch.dict(os.environ, {}, clear=True):
            paths = app.Paths.resolve(args)
        self.assertEqual(paths.config, ROOT / "config.toml")
        config = app.load_config(paths.config)
        self.assertGreaterEqual(len(config["names"]), 200, "ship hundreds of names")
        self.assertTrue(config["voices"], "the default voice profiles are user-editable")
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
        temp = tempfile.TemporaryDirectory(prefix="agent-comms-queue-test-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        paths = app.Paths(root / "config/config.toml", root / "state", root / "run")
        write_config(paths.config)
        env = {**TEST_PANE, "TUIOS_PANE_ID": "queue-pane"}
        epoch = seed_binding(paths, env, "Alice")
        server = app.Server(paths, app.load_config(paths.config), quiet=True)
        server.queue = asyncio.Queue(maxsize=1)
        listener = await asyncio.start_server(server.handle, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]

        async def send(text):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                await app.write_frame(writer, {"protocol_version": app.VERSION, "token": server.token,
                                               "command": "send", "name": "Alice", "epoch": epoch,
                                               "pane_env": env, "msg": text})
                return await app.read_frame(reader)
            finally:
                await app.close_writer(writer)

        try:
            with patch.object(server, "schedule_title_refresh"):
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
