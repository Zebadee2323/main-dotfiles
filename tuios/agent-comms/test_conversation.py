"""Display metadata, reply linkage, the silent conversation view, and shared lifecycle events."""
import asyncio
import io
import json
import time
import unittest
from unittest.mock import patch

import agent_comms as app
import conversation
from test_agent_comms import Fixture, write_config

EPOCH = "0123456789abcdef"
FACE_ROW, NAME_ROW, SESSION_ROW = 3, 5, 6


def resolve(request, sender="Alice", agents=("Alice", "Ben"), messages=None, user="Oli", uid="a" * 32):
    found = {name.lower(): name for name in agents}
    return conversation.resolve(request, sender=sender, epoch=EPOCH, message_id=uid, user_name=user,
                                find_agent=lambda name: found.get(name.lower()),
                                find_message=lambda mid: (messages or {}).get(mid))


class ResolveTests(unittest.TestCase):
    def test_new_exchange_defaults_to_configured_user(self):
        fields, warnings = resolve({})
        self.assertEqual(warnings, [])
        self.assertEqual(fields["recipient"], {"selector": None, "kind": "user", "name": "Oli", "fallback": False})
        self.assertEqual((fields["kind"], fields["emotion"], fields["speaker_role"]), ("statement", "neutral", "requester"))
        self.assertEqual(fields["exchange"]["requester"], {"kind": "agent", "name": "Alice", "epoch": EPOCH})
        self.assertEqual(fields["exchange"]["id"], "a" * 32)

    def test_explicit_user_agent_and_unknown_recipients(self):
        fields, _ = resolve({"display_recipient": "USER"})
        self.assertEqual(fields["recipient"]["kind"], "user")
        fields, _ = resolve({"display_recipient": "ben", "role": "responder", "kind": "query", "emotion": "curious"})
        self.assertEqual(fields["recipient"], {"selector": "ben", "kind": "agent", "name": "Ben", "epoch": EPOCH,
                                               "fallback": False})
        self.assertEqual(fields["exchange"]["requester"]["name"], "Ben")
        self.assertEqual(fields["exchange"]["responder"]["name"], "Alice")
        fields, warnings = resolve({"display_recipient": "Ghost"})
        self.assertEqual((fields["recipient"]["kind"], fields["recipient"]["fallback"]), ("user", True))
        self.assertIn("Ghost", warnings[0])

    def test_self_addressed_and_missing_user_name_are_errors(self):
        with self.assertRaisesRegex(conversation.ConversationError, "yourself"):
            resolve({"display_recipient": "alice"})
        with self.assertRaisesRegex(conversation.ConversationError, "user.name"):
            resolve({"kind": "query"}, user=None)
        fields, warnings = resolve({}, user=None)
        self.assertNotIn("exchange", fields)  # Plain sends never invent a participant.

    def test_invalid_metadata_is_rejected(self):
        for request, error in (({"kind": "shout"}, "--kind"), ({"role": "boss"}, "--role"),
                               ({"emotion": "x" * 40}, "--emotion"), ({"emotion": "\x1b[31m"}, "--emotion"),
                               ({"display_recipient": "Bad Name"}, "--display-recipient"),
                               ({"reply_to": "nope"}, "--reply-to")):
            with self.subTest(request=request), self.assertRaisesRegex(conversation.ConversationError, error):
                resolve(request)

    def test_reply_inherits_participants_and_validates_overrides(self):
        first, _ = resolve({"display_recipient": "Ben", "kind": "query"})
        record = {"uid": "a" * 32, "epoch": EPOCH, **first}
        messages = {"a" * 32: record}
        reply, warnings = resolve({"reply_to": "a" * 32, "emotion": "happy"}, sender="Ben", messages=messages,
                                  uid="b" * 32)
        self.assertEqual(warnings, [])
        self.assertEqual(reply["exchange"], first["exchange"])
        self.assertEqual(reply["speaker_role"], "responder")
        self.assertEqual(reply["recipient"]["name"], "Alice")
        again, _ = resolve({"reply_to": "a" * 32}, sender="Alice", messages=messages)
        self.assertEqual(again["speaker_role"], "requester")  # Either side may speak repeatedly.
        for request, sender, error in (({"reply_to": "a" * 32, "role": "requester"}, "Ben", "conflicts"),
                                       ({"reply_to": "a" * 32, "display_recipient": "user"}, "Ben", "conflicts"),
                                       ({"reply_to": "a" * 32}, "Cleo", "not a participant"),
                                       ({"reply_to": "c" * 32}, "Ben", "Unknown or expired")):
            with self.subTest(request=request), self.assertRaisesRegex(conversation.ConversationError, error):
                resolve(request, sender=sender, agents=("Alice", "Ben", "Cleo"), messages=messages)
        stale = {"a" * 32: {**record, "epoch": "f" * 16}}
        with self.assertRaisesRegex(conversation.ConversationError, "reset"):
            resolve({"reply_to": "a" * 32}, sender="Ben", messages=stale)


class RenderTests(unittest.TestCase):
    exchange = {"id": "x", "requester": {"kind": "agent", "name": "Alice"},
                "responder": {"kind": "agent", "name": "Ben"}}

    def message(self, **values):
        base = {"uid": "1", "id": "Alice", "name": "Alice", "msg": "Could you review this?", "phase": "displaying",
                "started_at": 100.0, "duration": 2.0, "kind": "query", "emotion": "curious",
                "speaker_role": "requester", "exchange": self.exchange}
        return {**base, **values}

    def text(self, view, width=72, height=14, now=101.0):
        return view.render(width, height, now)

    def test_faces_are_deterministic_and_unknown_falls_back(self):
        self.assertEqual(len(set(conversation.FACES.values())), len(conversation.FACES))
        for emotion in ("neutral", "happy", "sad", "concerned", "curious", "excited", "frustrated"):
            self.assertIn(emotion, conversation.FACES)
            self.assertTrue(conversation.FACES[emotion].isascii())
        self.assertEqual(conversation.face("bewildered"), conversation.FACES["neutral"])

    def test_sides_stay_fixed_while_emphasis_and_text_follow_the_speaker(self):
        view = conversation.ConversationView(color=False, animate=False)
        view.update(self.message(), 100.0)
        lines = self.text(view)
        self.assertLess(lines[NAME_ROW].index("Alice"), lines[NAME_ROW].index("Ben"))
        self.assertIn("(?_?)", lines[FACE_ROW][:14])
        self.assertIn("QUERY", lines[0])
        box = next(line for line in lines if "Could you review" in line)
        view.update(self.message(uid="2", name="Ben", msg="Looks good.", speaker_role="responder",
                                 emotion="happy", kind="statement"), 101.0)
        lines = self.text(view, now=101.5)
        self.assertLess(lines[NAME_ROW].index("Alice"), lines[NAME_ROW].index("Ben"))
        self.assertIn("(^_^)", lines[FACE_ROW][-14:])
        reply = next(line for line in lines if "Looks good." in line)
        self.assertGreater(reply.index("│"), box.index("│"), "text sits nearer the right-hand speaker")
        self.assertEqual(sum("╰" in line and "╯" in line and "─" * 20 in line for line in lines), 1)

    def test_slide_in_happens_once_per_exchange_and_respects_reduced_motion(self):
        view = conversation.ConversationView(color=False, animate=True)
        view.update(self.message(), 100.0)
        self.assertTrue(view.animating(100.1))
        self.assertNotIn("Alice", self.text(view, now=100.01)[NAME_ROW])
        self.assertIn("Alice", self.text(view, now=100.0 + conversation.SLIDE_SECONDS)[NAME_ROW])
        view.update(self.message(uid="2", name="Ben", speaker_role="responder"), 103.0)
        self.assertFalse(view.animating(103.0), "same exchange: no re-entry animation")
        view.update(self.message(uid="3", exchange={**self.exchange, "id": "y"}), 104.0)
        self.assertTrue(view.animating(104.0))
        still = conversation.ConversationView(color=False, animate=False)
        still.update(self.message(), 100.0)
        self.assertFalse(still.animating(100.0))
        self.assertIn("Alice", self.text(still, now=100.0)[NAME_ROW])

    def test_captions_follow_the_shared_timeline(self):
        view = conversation.ConversationView(color=False, animate=False)
        long = " ".join(f"word{i}" for i in range(80))
        view.update(self.message(msg=long, phase="preparing"), 100.0)
        self.assertFalse(any("word0" in line for line in self.text(view)), "never show text before display")
        view.update(self.message(msg=long), 100.0)
        early = "\n".join(self.text(view, now=100.01))
        late = "\n".join(self.text(view, now=101.99))
        self.assertIn("word0", early)
        self.assertNotIn("word79", early)
        self.assertIn("word79", late)

    def test_sessions_show_for_agents_and_human_for_the_user(self):
        view = conversation.ConversationView(color=False, animate=False)
        exchange = {"id": "s", "requester": {"kind": "user", "name": "Oli"},
                    "responder": {"kind": "agent", "name": "Ben", "session": "valkyrie"}}
        view.update(self.message(name="Ben", speaker_role="responder", exchange=exchange), 100.0)
        lines = self.text(view)
        self.assertIn("◆ human", lines[SESSION_ROW])
        self.assertIn("◆ valkyrie", lines[SESSION_ROW])
        # Without a snapshot, the latest session seen from that agent's own messages is used.
        exchange = {"id": "t", "requester": {"kind": "agent", "name": "Alice"},
                    "responder": {"kind": "agent", "name": "Ben"}}
        view.update(self.message(session_name="dotfiles", exchange=exchange), 101.0)
        lines = self.text(view, now=101.5)
        self.assertIn("◆ dotfiles", lines[SESSION_ROW])
        self.assertIn("◆ valkyrie", lines[SESSION_ROW])
        self.assertIn("◆ dotfiles", view.render(30, 6, 101.5)[1])

    def test_narrow_panes_idle_offline_and_sanitization(self):
        view = conversation.ConversationView(color=True, animate=False)
        hostile = "\x1b]52;c;evil\x07Mal\x1b[31mlory\u202e"
        view.update(self.message(name=hostile, msg="hi\x1b[2Jthere", exchange={
            "id": "z", "requester": {"kind": "agent", "name": hostile}, "responder": {"kind": "user", "name": "Oli"}}),
            100.0)
        for width, height in ((72, 14), (30, 5), (8, 2), (1, 1)):
            with self.subTest(size=(width, height)):
                lines = view.render(width, height, 101.0)
                self.assertEqual(len(lines), height)
                joined = "".join(lines)
                self.assertNotIn("evil", joined)
                self.assertNotIn("\x1b[2J", joined)
                self.assertNotIn("\x1b]", joined)
                self.assertNotIn("\u202e", joined)
        compact = conversation.ConversationView(color=False, animate=False)
        compact.update(self.message(), 100.0)
        self.assertIn("Alice ▸ Ben", compact.render(30, 6, 101.0)[0])
        idle = conversation.ConversationView(color=False)
        idle.update(None, 100.0)
        self.assertIn("idle", idle.render(72, 14, 100.0)[0])
        idle.update(None, 100.0, offline=True)
        self.assertIn("offline", idle.render(72, 14, 100.0)[0])


class LifecycleTests(Fixture):
    def frames(self, stream, count):
        return [json.loads(stream.readline()) for _ in range(count)]

    def test_lifecycle_events_are_ordered_and_late_joiners_get_a_snapshot(self):
        self.start_server()
        stream = self.subscribe()
        _, sender = self.request("send", **self.sender("Alice"), msg="first")
        ack = json.loads(sender.readline())
        self.assertRegex(ack["message_id"], r"^[0-9a-f]{32}$")
        late = self.subscribe()
        events = []
        while not events or events[-1][0] != "end":
            frame = json.loads(stream.readline())
            events.append((frame["lifecycle"]["event"], frame["lifecycle"]["message_id"], frame["lifecycle"]["seq"]))
        self.assertEqual([e for e, *_ in events], ["start", "display", "end"])
        self.assertTrue(all(mid == ack["message_id"] for _, mid, _ in events))
        self.assertEqual([s for *_, s in events], sorted({s for *_, s in events}))
        first = json.loads(late.readline())
        self.assertIn(first["lifecycle"]["event"], ("start", "display"))
        self.assertEqual(first["message"]["uid"], ack["message_id"])

    def test_conversation_view_is_silent_and_queue_advances_on_fallback_timing(self):
        write_config(self.paths.config, 'display_seconds = 0.3\n[user]\nname = "Oli"\n')
        self.start_server()
        watcher_output = io.StringIO()
        observer = self.subscribe()

        async def run():
            task = asyncio.create_task(app.watch_conversation(
                self.paths, color=False, animate=False, size=lambda: (72, 14), writer=watcher_output))
            await asyncio.sleep(0.5)
            for text in ("first message", "second message"):
                _, sender = await asyncio.to_thread(self.request, "send", **self.sender("Alice"), msg=text,
                                                    kind="query", emotion="happy")
                self.assertTrue(json.loads(await asyncio.to_thread(sender.readline))["ok"])
            await asyncio.sleep(2.0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        with patch.object(app, "watcher_speech", side_effect=AssertionError("conversation view must stay silent")), \
                patch.object(app.speech, "play_live", side_effect=AssertionError("no playback")):
            asyncio.run(run())
        displayed = []
        while len(displayed) < 2:
            frame = json.loads(observer.readline())
            message = frame["message"]
            if message and message.get("phase") == "displaying" and frame["lifecycle"]["event"] == "display":
                displayed.append(message)
        for message in displayed:
            self.assertNotIn("visualizer", message)  # Nobody claimed speech.
            self.assertGreaterEqual(message["duration"], 0.3)
        output = watcher_output.getvalue()
        self.assertIn("first message", output)
        self.assertIn("second message", output)
        self.assertIn("Alice", output)
        self.assertIn("Oli", output)
        self.assertIn("(^_^)", output)


if __name__ == "__main__":
    unittest.main()
