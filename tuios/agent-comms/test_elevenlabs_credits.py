"""Reported billing metadata, optional API access, and watcher rendering."""
import asyncio
import io
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import agent_comms as app
import speech
from test_agent_comms import Fixture


class CreditAPITests(unittest.TestCase):
    def test_generation_uses_reported_cost_including_fractional_and_zero(self):
        for header, expected in (("12.5", 12.5), ("0", 0), (None, None), ("nan", None), ("bad", None)):
            with self.subTest(header=header), tempfile.TemporaryDirectory() as directory:
                response = io.BytesIO(b'\0' * 480)
                response.headers = {"character-cost": header}
                with patch.object(speech.urllib.request, "urlopen", return_value=response):
                    audio = speech.synthesize(directory, "voice", "different text length", "secret")
                self.assertEqual(audio.credit_cost, expected)
                audio.remove()

    def test_subscription_balance_and_validation(self):
        for used, limit, expected in ((25, 100, 75), (150, 100, 0), (0.5, 100, 99.5)):
            response = io.BytesIO(json.dumps({"character_count": used, "character_limit": limit}).encode())
            with patch.object(speech.urllib.request, "urlopen", return_value=response) as request:
                self.assertEqual(speech.remaining_credits("secret"), expected)
            self.assertEqual(request.call_args.args[0].full_url, "https://api.elevenlabs.io/v1/user/subscription")
        for payload in (b'{}', b'[]', b'bad json', b'{"character_count": -1, "character_limit": 100}'):
            with patch.object(speech.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
                with self.assertRaises(speech.SpeechError):
                    speech.remaining_credits("secret")
        with patch.object(speech.urllib.request, "urlopen", side_effect=OSError("secret")):
            with self.assertRaisesRegex(speech.SpeechError, '^ElevenLabs credit balance unavailable.$'):
                speech.remaining_credits("secret")


class CreditLeaseTests(Fixture):
    def test_cost_is_shared_with_all_watchers_and_late_joiners(self):
        self.start_server()
        streams = [self.request("watch")[1] for _ in range(2)]
        for stream in streams:
            stream.readline()
        self.transport_send("a", "hello")
        messages = [json.loads(stream.readline())["message"] for stream in streams]
        uid = messages[0]["uid"]
        _, claim = self.request("claim-speech", uid=uid)
        lease = json.loads(claim.readline())["lease"]
        _, ready = self.request("speech-ready", uid=uid, lease=lease, duration=0.5, credit_cost=12.5)
        self.assertTrue(json.loads(ready.readline())["ok"])
        for stream in streams:
            message = json.loads(stream.readline())["message"]
            self.assertEqual(message["phase"], "displaying")
            self.assertEqual(message["credit_cost"], 12.5)
        _, late = self.request("watch")
        self.assertEqual(json.loads(late.readline())["message"]["credit_cost"], 12.5)


class WatcherCreditTests(unittest.IsolatedAsyncioTestCase):
    async def render(self, mode, balance=12345, width=80, key="fake"):
        response = {"event": "state", "message": {"id": "a", "name": "Alice", "msg": "Hello world",
                    "uid": "m", "phase": "displaying", "duration": 5, "credit_cost": 12.5}}
        output = io.StringIO()
        blocked = asyncio.Event()
        async def read(*args, **kwargs):
            await blocked.wait()
        with patch.dict(app.os.environ, {"ELEVENLABS_API_KEY": key}), \
             patch.object(app, "connect", AsyncMock(return_value=(None, None, response))), \
             patch.object(app, "read_frame", side_effect=read), \
             patch.object(speech, "remaining_credits", side_effect=(speech.SpeechError() if balance is None else None),
                          return_value=balance) as usage, \
             patch('sys.stdout', output):
            task = asyncio.create_task(app.watch_stream(None, color=False, width=width, muted=True,
                                      settings={"mode": mode}, flash=False))
            try:
                for _ in range(100):
                    if "Hello" in output.getvalue() and (usage.call_count or mode != 'elevenlabs' or not key):
                        await asyncio.sleep(0.06)
                        break
                    await asyncio.sleep(0.01)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return output.getvalue(), usage.call_count

    async def test_balance_cost_width_and_other_modes(self):
        output, calls = await self.render("elevenlabs")
        self.assertIn("12,345 | 12.5", output)
        self.assertGreater(calls, 0)
        self.assertTrue(all(speech.width(line) <= 80 for line in output.splitlines()))
        for mode in ("phonon", "pocket_tts"):
            output, calls = await self.render(mode)
            self.assertNotIn("12,345", output)
            self.assertEqual(calls, 0)
        output, calls = await self.render("elevenlabs", key="")
        self.assertIn("?", output)
        self.assertEqual(calls, 0)
        output, _ = await self.render("elevenlabs", balance=None)
        self.assertIn("?", output)
        output, _ = await self.render("elevenlabs", width=40)
        self.assertTrue(all(speech.width(line) <= 40 for line in output.splitlines()))
