import asyncio
from array import array
import base64
from pathlib import Path
import tempfile
import wave

import audio_visualizer
import re
import time
import unittest
from unittest.mock import AsyncMock, patch

import agent_comms as app


SGR = re.compile(r"\x1b\[[0-9;]*m")


class DeliveryRenderingTests(unittest.TestCase):
    def setUp(self):
        self.message = {"id": "a", "name": "Alice", "msg": "Readable caption", "phase": "displaying"}

    def test_real_audio_glows_behind_unchanged_text(self):
        visualizer = {"levels": base64.b64encode(bytes(range(100))).decode(), "step": 0.02}
        message = {**self.message, "visualizer": visualizer, "visual_elapsed": 1.8}
        line = app.dock_line(message, frame=0, width=29)
        self.assertEqual(SGR.sub("", line), app.dock_line(message, color=False, width=29))
        self.assertTrue(line.startswith("\x1b[49m"))
        self.assertIn("48;2;", line)
        self.assertNotEqual(line, app.dock_line({**message, "visual_elapsed": 1}, frame=1, width=29))

    def test_silence_and_missing_audio_never_invent_activity(self):
        for visualizer in (None, {"levels": base64.b64encode(bytes(100)).decode(), "step": 0.02}):
            first = app.dock_line({**self.message, "visualizer": visualizer, "visual_elapsed": 0.5}, frame=1)
            later = app.dock_line({**self.message, "visualizer": visualizer, "visual_elapsed": 1.5}, frame=30)
            self.assertEqual(first, later)
            self.assertEqual(app.cell_width(SGR.sub("", first)), 96)
            self.assertIn("Readable caption", SGR.sub("", first))
            self.assertNotIn("48;", first)

    def test_live_volume_changes_only_color_and_preserves_spaces(self):
        message = {**self.message, "visualizer": {"levels": base64.b64encode(bytes([180] * 100)).decode(), "step": 0.02},
                   "visual_elapsed": 1.9}
        loud = app.dock_line(message, frame=0, width=80)
        silent = app.dock_line({**message, "visual_volume": 0}, frame=0, width=80)
        expected = app.dock_line(message, color=False, width=80).ljust(80)
        self.assertEqual(SGR.sub("", loud), expected)
        self.assertEqual(SGR.sub("", silent), expected)
        self.assertNotEqual(loud, silent)
        self.assertIn("48;2;", loud)
        self.assertNotIn("48;", silent)

    def test_analysis_measures_silence_and_pcm_energy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "voice.wav"
            with wave.open(str(path), "wb") as output:
                output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                import math
                samples = array('h', [0] * 2400 + [round(12000 * math.sin(2 * math.pi * 1000 * n / 24000)) for n in range(2400)])
                import sys
                if sys.byteorder != "little":
                    samples.byteswap()
                output.writeframes(samples.tobytes())
            levels, step, bands = audio_visualizer.decode(audio_visualizer.analyse(path))
            self.assertEqual(step, 0.02)
            self.assertEqual(bands, 8)
            self.assertEqual(levels[:5 * bands], bytes(5 * bands))
            self.assertTrue(any(value > 0 for value in levels[5 * bands:]))

    def test_fft_separates_low_mid_and_high_tones(self):
        import math
        for frequency, expected_band in ((180, 1), (750, 3), (2400, 5)):
            samples = array('h', (round(20000 * math.sin(2 * math.pi * frequency * n / 24000)) for n in range(960)))
            measured = audio_visualizer.spectrum(samples, 1, 24000)
            self.assertEqual(max(range(8), key=measured.__getitem__), expected_band)

    def test_only_audible_columns_receive_background_color(self):
        # Audio in the most recent second: left-hand silence stays transparent.
        data = (bytes(50) + bytes([180] * 50), 0.02, 1)
        text = "Audio reactive caption across the bar"
        line = audio_visualizer.render(text, 40, "36", data, 2)
        self.assertEqual(SGR.sub("", line), text.ljust(40))
        columns = re.findall(r"\x1b\[(49|48;2;[0-9;]+)m([^\x1b])", line)
        self.assertEqual(len(columns), 40)
        self.assertTrue(all(style == "49" for style, _ in columns[:19]))
        self.assertTrue(all(style.startswith("48;2;") for style, _ in columns[-19:]))
        self.assertNotIn("48;2;0;0;0", line)

    def test_transparency_includes_arrival_flash_and_badges(self):
        for highlight in (True, False):
            line = app.dock_line(self.message, frame=0, highlight=highlight, lead="[Volume 50%] ")
            self.assertNotIn("48;", line)
            self.assertNotIn("[7;", line)
            self.assertIn("49", line)

    def test_invalid_or_oversized_metadata_is_ignored(self):
        for value in (None, {}, {"levels": "!", "step": 0.02},
                      {"levels": "AA==", "step": float('nan')},
                      {"levels": "A" * 20004, "step": 0.02}):
            self.assertIsNone(audio_visualizer.decode(value))

    def test_tiny_widths_and_untrusted_text_remain_safe(self):
        message = {**self.message, "name": "Alice\x1b[2J", "msg": "Readable\ncaption\x1b]0;title\x07"}
        for preparing in (True, False):
            for width in range(1, 16):
                line = app.dock_line({**message, "phase": "preparing" if preparing else "displaying"},
                                     width=width, frame=5)
                plain = SGR.sub("", line)
                self.assertLessEqual(app.cell_width(plain), width)
                self.assertNotIn("\x1b", plain)
                self.assertNotIn("\n", plain)

    def test_plain_idle_and_offline_remain_static(self):
        for message, options in ((self.message, {"color": False}), (None, {}), (self.message, {"offline": True})):
            self.assertEqual(app.dock_line(message, frame=0, **options), app.dock_line(message, frame=9, **options))

    def test_unicode_truncation_remains_within_cell_budget(self):
        message = {**self.message, "name": "界e\u0301", "msg": "界" * 100}
        for width in (16, 30, 80):
            for frame in range(12):
                plain = SGR.sub("", app.dock_line(message, width=width, frame=frame))
                self.assertLessEqual(app.cell_width(plain), width)
                self.assertTrue(plain.rstrip().endswith("..."))

    def test_cli_effects_switches(self):
        self.assertTrue(app.parser().parse_args(["watch"]).vfx)
        self.assertTrue(app.parser().parse_args(["watch", "--vfx"]).vfx)
        args = app.parser().parse_args(["watch", "--no-vfx", "--no-flash"])
        self.assertFalse(args.vfx)
        self.assertFalse(args.flash)


class DeliveryPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_effects_continue_after_arrival_flash_and_can_be_disabled(self):
        for vfx in (True, False):
            with self.subTest(vfx=vfx):
                now = time.monotonic()
                message = {"id": "a", "name": "Alice", "msg": "Readable caption", "phase": "displaying",
                           "started_at": now - 1, "duration": 10}
                state = {"message": message, "arrived": now - 1, "changed": asyncio.Event()}
                output = []
                ready = asyncio.Event()

                def emit(message, **options):
                    output.append((message, options))
                    if len(output) >= 5:
                        ready.set()

                with patch.object(app, "watcher_speech", new=AsyncMock()):
                    task = asyncio.create_task(app.present_message(None, state, {}, emit, 80, True, True, vfx))
                    try:
                        await asyncio.wait_for(ready.wait(), 2)
                    finally:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(all(not options["highlight"] for _, options in output))
                frames = [options["frame"] for _, options in output]
                if vfx:
                    self.assertGreater(len(set(frames)), 1)
                else:
                    self.assertEqual(set(frames), {None})


if __name__ == "__main__":
    unittest.main()
