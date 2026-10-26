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
        self.assertTrue(SGR.sub("", app.dock_line(preparing, frame=0)).startswith("[·  ]"))
        self.assertTrue(SGR.sub("", app.dock_line(self.message, frame=0)).startswith("▁▃▅▃▁"))

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
