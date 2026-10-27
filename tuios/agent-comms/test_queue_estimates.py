"""Queue-aware completion estimates, without speech generation or playback."""
import json
from unittest.mock import patch

import agent_comms as app
from test_agent_comms import Fixture


class QueueEstimateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.server = app.Server(self.paths, app.load_config(self.paths.config), quiet=True)

    def message(self, text="hello world", **fields):
        return {"msg": "Caption text that should not determine speech time", "tts": text, **fields}

    def test_idle_and_pending_messages_include_point_65_seconds_each(self):
        self.assertEqual(self.server.estimated_total_seconds(), 0)
        self.server.pending = {"a": self.message(), "b": self.message("e\u0301 界")}
        self.assertAlmostEqual(self.server.estimated_total_seconds(), 1.3 + 12 / 15)

    def test_displaying_uses_actual_remaining_duration_not_full_message(self):
        self.server.current = self.message(phase="displaying", started_at=95, duration=10)
        self.server.audio_duration = 10
        self.server.pending = {"next": self.message()}
        with patch.object(app.time, "monotonic", return_value=100):
            self.assertAlmostEqual(self.server.estimated_total_seconds(), 5.15 + 0.65 + 10 / 15)
        with patch.object(app.time, "monotonic", return_value=200):
            self.assertAlmostEqual(self.server.estimated_total_seconds(), 0.65 + 10 / 15)

    def test_preparing_subtracts_elapsed_overhead_but_preserves_speech(self):
        self.server.current = self.message(phase="preparing")
        self.server.preparing_started_at = 100
        for now, overhead in ((100, 0.65), (100.4, 0.25), (103, 0)):
            with self.subTest(now=now), patch.object(app.time, "monotonic", return_value=now):
                self.assertAlmostEqual(self.server.estimated_total_seconds(), overhead + 10 / 15)

    def test_muted_queue_uses_caption_timing_without_generation_overhead(self):
        self.server.audio_muted = True
        self.server.config["display_seconds"] = 5
        self.server.current = self.message(phase="preparing", msg="hi")
        self.server.pending = {"a": self.message(msg="hi")}
        self.assertEqual(self.server.estimated_total_seconds(), 10)

    def test_listener_response_includes_new_message_and_existing_queue(self):
        self.start_server()
        # Claim speech to hold the first message in preparation while sending more.
        _, watcher = self.request("watch")
        watcher.readline()
        self.transport_send("a", "hello world")
        preparing = json.loads(watcher.readline())["message"]
        _, claim = self.request("claim-speech", uid=preparing["uid"])
        self.assertTrue(json.loads(claim.readline())["claimed"])
        totals = []
        for _ in range(2):
            _, stream = self.request("send", id="a", msg="hi", tts="hello world")
            response = json.loads(stream.readline())
            self.assertTrue(response["ok"], response)
            totals.append(response["estimated_total_seconds"])
        # The second reply adds another full speech estimate plus overhead;
        # elapsed preparation between requests can only reduce the difference.
        self.assertGreater(totals[0], 0.65 + 10 / 15)
        self.assertGreater(totals[1] - totals[0], 0.95)
        self.assertLessEqual(totals[1] - totals[0], 0.65 + 10 / 15 + 0.001)
