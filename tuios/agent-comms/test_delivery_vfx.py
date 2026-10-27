import asyncio
import re
import time
import unittest
from unittest.mock import AsyncMock, patch

import agent_comms as app


SGR = re.compile(r"\x1b\[[0-9;]*m")


class DeliveryRenderingTests(unittest.TestCase):
    def setUp(self):
        self.message = {"id": "a", "name": "Alice", "msg": "Readable caption", "phase": "displaying"}

    def test_waveform_and_glow_move_without_changing_caption_or_width(self):
        frames = [app.dock_line(self.message, frame=frame) for frame in range(24)]
        self.assertEqual(len(set(frames)), len(frames))
        for line in frames:
            plain = SGR.sub("", line)
            self.assertTrue(plain.endswith(" Alice: Readable caption"))
            self.assertEqual(app.cell_width(plain), app.cell_width(app.dock_line(self.message, color=False)))
            self.assertNotIn("\x1b", plain)
            self.assertNotIn("\n", plain)
            self.assertIn("\x1b[0m Readable caption", line)

    def test_preparation_orbit_then_delivery_waveform(self):
        preparing = {**self.message, "phase": "preparing"}
        self.assertTrue(SGR.sub("", app.dock_line(preparing, frame=0)).startswith("●•···"))
        badge = SGR.sub("", app.dock_line(self.message, frame=0))[:5]
        self.assertTrue(all(char in "▁▂▃▄▅▆▇█" for char in badge))

    def test_spectrum_bands_have_independent_heights_and_colors(self):
        frames = [app.delivery_prefix("[ELE] Alice:", frame, "36", False) for frame in range(80)]
        badges = [SGR.sub("", frame)[:5] for frame in frames]
        self.assertGreater(len(set(badges)), 50)
        self.assertGreater(len(set(SGR.findall(frames[0])[:5])), 2)
        self.assertTrue(any("█" in badge for badge in badges))
        self.assertTrue(any("▁" in badge for badge in badges))

    def test_spectrum_and_scanner_follow_identity_palette(self):
        for preparing in (True, False):
            cyan = app.delivery_prefix("[ELE] Alice:", 3, "36", preparing)
            pink = app.delivery_prefix("[ELE] Alice:", 3, "35", preparing)
            self.assertEqual(SGR.sub("", cyan), SGR.sub("", pink))
            self.assertNotEqual(cyan, pink)

    def test_scanner_travels_in_both_directions(self):
        positions = [SGR.sub("", app.delivery_prefix("[ELE] Alice:", frame, "36", True)).index("●")
                     for frame in range(9)]
        self.assertEqual(positions, [0, 1, 2, 3, 4, 3, 2, 1, 0])

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
                self.assertTrue(plain.endswith("..."))

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
