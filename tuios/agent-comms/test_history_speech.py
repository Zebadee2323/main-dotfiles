import asyncio
from contextlib import suppress
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch
import wave

import agent_comms as app
import message_history
import speech
import tuios_context
from test_agent_comms import Fixture, SCRIPT


class HistoryTests(Fixture):
    def test_accepted_messages_persist_and_print_from_any_directory_while_offline(self):
        server, _ = self.start_server()
        for identity, text in (("a", "first"), ("b", "second"), ("a", "third")):
            result = self.transport_send(identity, text, "[warm] " + text)
            self.assertEqual(result.returncode, 0, result.stderr)
        server.kill()
        server.wait(timeout=5)
        result = subprocess.run([sys.executable, str(SCRIPT), "history", "--limit", "2", "--json"],
                                cwd=self.temp.name, env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = json.loads(result.stdout)
        self.assertEqual([record["msg"] for record in records], ["second", "third"])
        self.assertEqual(records[-1]["tts"], "[warm] third")
        self.assertIn("created_at", records[-1])
        self.assertEqual(len({record["uid"] for record in records}), 2)
        filtered = self.cli("history", "--id", "a")
        self.assertIn("Alice", filtered.stdout)
        self.assertIn("first", filtered.stdout)
        self.assertIn("third", filtered.stdout)
        self.assertNotIn("Bob", filtered.stdout)
        if os.name != "nt":
            self.assertEqual((self.paths.state / "history.jsonl").stat().st_mode & 0o777, 0o600)

    def test_empty_history_needs_neither_config_nor_server(self):
        self.paths.config.unlink()
        result = self.cli("history", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])
        self.assertFalse(self.paths.state.exists())

    def test_pretty_default_raw_and_json_formats(self):
        self.paths.state.mkdir()
        record = {"id": "a", "name": "Alice", "msg": "First line\nSecond line " + "word " * 30,
                  "tts": "Spoken text", "created_at": "2026-10-26T12:34:56+00:00",
                  "session_name": "work", "workspace": 2}
        message_history.append(self.paths.state, record)
        pretty = self.cli("history")
        self.assertEqual(pretty.returncode, 0, pretty.stderr)
        self.assertIn("Agent communications · 1 message", pretty.stdout)
        self.assertIn("Alice · work / workspace 2", pretty.stdout)
        self.assertIn("    First line\n    Second line", pretty.stdout)
        self.assertNotIn("\x1b", pretty.stdout, "Redirected output must not contain ANSI")
        raw = self.cli("history", "--raw")
        self.assertEqual(raw.stdout, "2026-10-26T12:34:56+00:00 Alice: " + app.clean_text(record["msg"]) + "\n")
        self.assertEqual(json.loads(self.cli("history", "--json").stdout), [record])
        self.assertEqual(self.cli("history", "--json", "--raw").returncode, 2)

    def test_history_rendering_wraps_safely_and_handles_empty_history(self):
        self.assertEqual(self.cli("history").stdout, "No messages in history.\n")
        self.assertEqual(self.cli("history", "--raw").stdout, "")
        record = {"id": "a", "name": "\x1b[31mAlice", "msg": "界" * 60 + "\x1b[2J",
                  "created_at": "invalid timestamp"}
        text = app.format_history([record], width=40)
        self.assertNotIn("\x1b", text)
        self.assertTrue(all(speech.width(line) <= 40 for line in text.splitlines()))
        self.assertIn("\x1b[1;36m", app.format_history([record], color=True))

    def test_rejected_messages_do_not_enter_history(self):
        self.start_server()
        _, stream = self.request("send", id="unknown", msg="rejected")
        self.assertFalse(json.loads(stream.readline())["ok"])
        self.assertEqual(json.loads(self.cli("history", "--json").stdout), [])

    def test_history_failure_rejects_instead_of_accepting_an_unrecorded_message(self):
        self.start_server()
        self.paths.state.mkdir(exist_ok=True)  # Lifecycle logging creates state at startup.
        (self.paths.state / "history.jsonl").mkdir()
        _, stream = self.request("send", id="a", msg="must not disappear")
        result = json.loads(stream.readline())
        self.assertFalse(result["ok"])
        self.assertIn("not accepted", result["error"])
        _, subscriber = self.request("watch")
        self.assertIsNone(json.loads(subscriber.readline())["message"])

    def test_explicit_display_and_tts_introductions_are_recorded_without_rewriting(self):
        self.start_server()
        source = {"session_name": "other work", "workspace": 3, "pane_id": "pane", "introduce": True}
        _, stream = self.request("send", id="a", msg="Alice here, of the other work session. Readable update",
                                 tts="Alice here, of the other work session. Spoken update", source=source)
        self.assertTrue(json.loads(stream.readline())["ok"])
        records = json.loads(self.cli("history", "--session", "other work", "--json").stdout)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["msg"], "Alice here, of the other work session. Readable update")
        self.assertEqual(records[0]["tts"], "Alice here, of the other work session. Spoken update")
        self.assertEqual(records[0]["workspace"], 3)

    def test_jsonl_recovers_torn_tail_and_reads_across_blocks(self):
        self.paths.state.mkdir()
        first = {"uid": "one", "id": "a", "name": "Alice", "msg": "x" * 20000, "created_at": "now"}
        message_history.append(self.paths.state, first)
        path = self.paths.state / "history.jsonl"
        with path.open("ab") as stream:
            stream.write(b'{"torn":')
        self.assertEqual(message_history.read(self.paths.state), [first])
        second = {**first, "uid": "two", "msg": "second"}
        message_history.append(self.paths.state, second)
        self.assertEqual(message_history.read(self.paths.state, limit=1), [second])
        self.assertEqual(message_history.read(self.paths.state), [first, second])

    def test_history_rolls_at_ten_entries_on_disk_and_filters_within_that_window(self):
        self.paths.state.mkdir()
        for index in range(15):
            record = {"uid": str(index), "id": "a" if index < 5 else "b", "name": "Agent",
                      "msg": str(index), "created_at": "now"}
            message_history.append(self.paths.state, record)
        path = self.paths.state / "history.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), 10)
        self.assertEqual([r["msg"] for r in message_history.read(self.paths.state)], [str(i) for i in range(5, 15)])
        self.assertEqual(message_history.read(self.paths.state, identity="a"), [])
        self.assertEqual([r["msg"] for r in message_history.read(self.paths.state, limit=2)], ["13", "14"])
        self.assertEqual(self.cli("history", "--limit", "11").returncode, 1)


class ContextTests(unittest.TestCase):
    env = {"TUIOS_ENV": "1", "TUIOS_PANE_ID": "own", "TUIOS_SESSION": "work"}

    def inspect(self, *, attached=True, workspace=1, current=1, focused="own", other_agent=False):
        windows = [{"window_id": "own", "workspace": workspace, "agent_state": "working"},
                   {"window_id": "other", "workspace": workspace, "agent_state": "idle" if other_agent else "none"}]
        info = {"session_name": "work", "tui_attached": attached}
        listing = {"windows": windows, "current_workspace": current, "focused_window_id": focused}
        with patch.object(tuios_context.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(info)),
            subprocess.CompletedProcess([], 0, json.dumps(listing)),
        ]) as run:
            result = tuios_context.context("Alice", run=run, env=self.env)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.kwargs["timeout"] == 2 for call in run.call_args_list))
        return result

    def test_introduce_when_session_detached_or_workspace_not_current(self):
        self.assertTrue(self.inspect(attached=False, other_agent=True)["introduce"])
        self.assertTrue(self.inspect(workspace=2, current=1, other_agent=True)["introduce"])

    def test_only_other_agent_panes_require_an_unfocused_introduction(self):
        self.assertTrue(self.inspect(focused="other", other_agent=True)["introduce"])
        self.assertFalse(self.inspect(focused="other", other_agent=False)["introduce"])
        self.assertFalse(self.inspect(focused="own", other_agent=True)["introduce"])

    def test_outside_tuios_or_forbidden_query_never_retries_or_blocks(self):
        with patch.object(tuios_context.subprocess, "run") as run:
            self.assertEqual(tuios_context.context("Alice", run=run, env={}), {})
            run.assert_not_called()
            run.side_effect = subprocess.CalledProcessError(1, ["tuios"], stderr="forbidden")
            self.assertEqual(tuios_context.context("Alice", run=run, env=self.env), {})
            self.assertEqual(run.call_count, 1)


class CaptionTests(unittest.TestCase):
    def test_naia_estimator_and_character_weighted_pages(self):
        self.assertAlmostEqual(speech.estimate_seconds("hello world"), 10 / 15)
        self.assertAlmostEqual(speech.estimate_seconds("e\u0301 界"), 2 / 15)
        pages = speech.pages("hello world friend", 8)
        self.assertEqual(pages, [("hello", 5), ("world", 10), ("friend", 16)])
        self.assertEqual(speech.page_at(pages, 4.9, 16), "hello")
        self.assertEqual(speech.page_at(pages, 5, 16), "world")
        self.assertEqual(speech.page_at(pages, 100, 16), "friend")

    def test_unicode_and_oversized_paths_are_not_lost(self):
        text = "/very/long/path/with/no/spaces/界界e\u0301"
        pages = speech.pages(text, 8)
        self.assertEqual("".join(page for page, _ in pages), text)
        self.assertTrue(all(speech.width(page) <= 8 for page, _ in pages))
        self.assertFalse(any(page.startswith("\u0301") for page, _ in pages))

    def test_flash_is_ansi_only_and_resets_style(self):
        message = {"id": "a", "name": "Alice", "msg": "new message"}
        flashed = app.dock_line(message, highlight=True)
        self.assertIn("\x1b[7;1m", flashed)
        self.assertTrue(flashed.endswith("\x1b[0m"))
        self.assertNotIn("\x1b", app.dock_line(message, color=False, highlight=True))


class SpeechTests(Fixture):
    def test_elevenlabs_uses_voice_key_tags_and_measured_duration(self):
        pcm = b"\0\0" * speech.SAMPLE_RATE
        response = io.BytesIO(pcm)
        with patch.object(speech.urllib.request, "urlopen", return_value=response) as request:
            audio = speech.synthesize(self.temp.name, "voice/id", "[warm] Hello!", "fake-test-key")
        self.addCleanup(audio.remove)
        sent = request.call_args.args[0]
        self.assertIn("/voice%2Fid?output_format=pcm_24000", sent.full_url)
        self.assertEqual(sent.get_header("Xi-api-key"), "fake-test-key")
        body = json.loads(sent.data)
        self.assertEqual(body["text"], "[warm] Hello!")
        self.assertEqual(body["model_id"], "eleven_turbo_v2_5")
        self.assertAlmostEqual(audio.duration, 1)
        with wave.open(str(audio.path)) as stream:
            self.assertEqual(stream.getnframes(), speech.SAMPLE_RATE)

    def test_network_errors_do_not_expose_keys_or_spoken_text(self):
        error = speech.urllib.error.URLError("private diagnostic containing a key")
        with patch.object(speech.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(speech.SpeechError) as raised:
                speech.synthesize(self.temp.name, "voice", "sensitive text", "secret")
        self.assertEqual(str(raised.exception), "ElevenLabs request failed.")
        self.assertEqual(list(Path(self.temp.name).glob("speech-*.wav")), [])

    def test_invalid_audio_is_rejected(self):
        with patch.object(speech.urllib.request, "urlopen", return_value=io.BytesIO(b"odd")):
            with self.assertRaises(speech.SpeechError):
                speech.synthesize(self.temp.name, "voice", "hello", "fake")


class WatcherIntegrationTests(Fixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        root = app.private_dir(Path(self.temp.name) / "temp-audio")
        root_patch = patch.object(app.AudioWorkspace, "root", return_value=root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

    async def test_multiple_watchers_generate_once_and_share_audio_timed_captions(self):
        self.paths.config.write_text('display_seconds = 0.1\n[identities.a]\nname="Alice"\neleven_labs_voice_id="test-voice"\n')
        self.start_server()
        lines = asyncio.Queue()
        paths = []

        def generate(directory, *args):
            filename = Path(directory) / "test.wav"
            filename.write_bytes(b"fake")
            paths.append(filename)
            return speech.Audio(filename, 0.7)

        async def playback(audio, executable):
            await asyncio.sleep(audio.duration)

        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "fake-key"}), \
             patch.object(speech, "synthesize", side_effect=generate) as synthesize, \
             patch.object(speech, "player", return_value="fake-player"), \
             patch.object(speech, "play", side_effect=playback) as play, \
             patch("builtins.print", side_effect=lambda line, **_: lines.put_nowait(line)):
            watchers = [asyncio.create_task(app.watch(self.paths, color=True, width=40)) for _ in range(2)]
            try:
                # Wait for both subscriptions before submitting a message.
                for _ in range(2):
                    self.assertIn("idle", await asyncio.wait_for(lines.get(), 3))
                reader, writer, _ = await app.connect(self.paths, "watch")
                await app.rpc(self.paths, "send", id="a", msg="A readable message with enough words for several dock caption pages.", tts="[warm] A spoken message.")
                while True:
                    message = (await app.read_frame(reader))["message"]
                    if message and message.get("phase") == "displaying":
                        break
                self.assertAlmostEqual(message["duration"], 0.7)
                self.assertEqual(message["tts"], "[warm] A spoken message.")
                self.assertEqual((await app.read_frame(reader))["message"], None)
                await app.close_writer(writer)
                output = []
                while not lines.empty():
                    output.append(lines.get_nowait())
                self.assertTrue(any("\x1b[7;1m" in line for line in output))
                self.assertTrue(any("several dock caption" in line or "caption pages" in line for line in output))
                self.assertEqual(synthesize.call_count, 1)
                self.assertEqual(play.call_count, 1)
            finally:
                for task in watchers:
                    task.cancel()
                await asyncio.gather(*watchers, return_exceptions=True)
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertTrue(all(not path.parent.exists() for path in paths))
        self.assertFalse((self.paths.state / "audio").exists())

    async def test_cancel_during_synthesis_keeps_workspace_until_worker_finishes(self):
        started, closed = asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        directories = []
        original_close = app.AudioWorkspace.close

        def generate(directory, *args):
            directories.append(directory)
            loop.call_soon_threadsafe(started.set)
            release.wait(timeout=5)
            path = directory / "speech-late.wav"
            path.write_bytes(b"fake")
            return speech.Audio(path, 1)

        def close(workspace):
            original_close(workspace)
            closed.set()

        state = {"message": {"uid": "m", "phase": "preparing", "voice_id": "v", "tts": "test"}}
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "fake"}), \
             patch.object(app, "rpc", new=AsyncMock(return_value={"claimed": True, "lease": "test"})), \
             patch.object(speech, "player", return_value="fake"), \
             patch.object(speech, "synthesize", side_effect=generate), \
             patch.object(speech, "play", new=AsyncMock()) as play, \
             patch.object(app.AudioWorkspace, "close", close):
            task = asyncio.create_task(app.watcher_speech(self.paths, state, {}, False))
            try:
                await asyncio.wait_for(started.wait(), 3)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(directories[0].exists())
                release.set()
                await asyncio.wait_for(closed.wait(), 3)
                self.assertFalse(directories[0].exists())
                play.assert_not_called()
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_no_key_or_mute_never_generates_speech(self):
        message = {"uid": "m", "id": "a", "name": "Alice", "msg": "Hello", "tts": "[warm] Hello",
                   "voice_id": "voice", "phase": "preparing"}
        state = {"message": message, "changed": asyncio.Event()}
        with patch.object(speech, "synthesize") as synthesize, patch.object(speech, "player", return_value="fake"):
            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": ""}):
                await app.watcher_speech(self.paths, state, {}, False)
            with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "fake"}):
                await app.watcher_speech(self.paths, state, {}, True)
            synthesize.assert_not_called()

    async def test_no_flash_disables_attention_without_disabling_captions(self):
        message = {"uid": "m", "id": "a", "name": "Alice", "msg": "A long readable caption with several words.",
                   "tts": "A long readable caption", "voice_id": "", "phase": "displaying",
                   "started_at": time.monotonic(), "duration": 1}
        for flash in (False, True):
            frames, enough = [], asyncio.Event()

            def emit(message, **kwargs):
                frames.append((message, kwargs))
                if len(frames) == 2:
                    enough.set()

            state = {"message": message, "changed": asyncio.Event(), "arrived": time.monotonic()}
            presentation = asyncio.create_task(app.present_message(self.paths, state, {}, emit, 40, True, flash))
            try:
                await asyncio.wait_for(enough.wait(), 1)
                self.assertEqual(frames[0][1]["highlight"], flash)
                self.assertIn("A long", frames[0][0]["msg"])
            finally:
                presentation.cancel()
                await asyncio.gather(presentation, return_exceptions=True)

    async def test_claim_is_exclusive_and_failed_speech_falls_back(self):
        self.start_server()
        reader, writer, _ = await app.connect(self.paths, "watch")
        await app.rpc(self.paths, "send", id="a", msg="hello")
        active = (await app.read_frame(reader))["message"]
        claim = await app.rpc(self.paths, "claim-speech", uid=active["uid"])
        self.assertTrue(claim["claimed"])
        self.assertFalse((await app.rpc(self.paths, "claim-speech", uid=active["uid"]))["claimed"])
        with self.assertRaises(app.CommsError):
            await app.rpc(self.paths, "speech-ready", uid=active["uid"], lease="wrong", duration=1)
        await app.rpc(self.paths, "speech-ready", uid=active["uid"], lease=claim["lease"], duration=None)
        displayed = (await app.read_frame(reader))["message"]
        self.assertEqual(displayed["phase"], "displaying")
        self.assertGreaterEqual(displayed["duration"], speech.estimate_seconds("hello"))
        await app.close_writer(writer)
