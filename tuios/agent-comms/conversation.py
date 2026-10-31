"""Display-only conversation metadata and the silent, pane-sized conversation view.

Recipients, roles, kinds, and emotions describe how an accepted message is shown.
They never route or deliver messages, and never change the verified sender.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

import speech

KINDS = ("instruction", "query", "statement")
ROLES = ("requester", "responder")
FACES = {
    "neutral": "(o_o)",
    "happy": "(^_^)",
    "sad": "(T_T)",
    "concerned": "(o_O)",
    "curious": "(?_?)",
    "excited": "(^o^)",
    "frustrated": "(>_<)",
}
EMOTION = re.compile(r"[a-z][a-z_-]{0,31}")
MESSAGE_ID = re.compile(r"[0-9a-f]{32}")
USER_SELECTOR = "user"
SLIDE_SECONDS = 0.45
PALETTE = ("36", "35", "32", "34", "33")


class ConversationError(ValueError):
    """Invalid display metadata; the message is rejected without queueing."""


def normalize_emotion(value):
    if value is None:
        return "neutral"
    if not isinstance(value, str) or not EMOTION.fullmatch(value.strip().lower()):
        raise ConversationError("--emotion must be 1-32 lowercase letters, '-' or '_' (e.g. happy, curious).")
    return value.strip().lower()


def face(emotion):
    """Deterministic ASCII expression; unknown emotions fall back to neutral."""
    return FACES.get(emotion, FACES["neutral"])


def validate_request(request):
    """Shape checks shared by client and listener. Returns normalized optional fields."""
    kind, role = request.get("kind"), request.get("role")
    if kind is not None and kind not in KINDS:
        raise ConversationError(f"--kind must be one of: {', '.join(KINDS)}.")
    if role is not None and role not in ROLES:
        raise ConversationError(f"--role must be one of: {', '.join(ROLES)}.")
    recipient = request.get("display_recipient")
    if recipient is not None and (not isinstance(recipient, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", recipient)):
        raise ConversationError("--display-recipient must be a generated agent name or 'user'.")
    reply = request.get("reply_to")
    if reply is not None and (not isinstance(reply, str) or not MESSAGE_ID.fullmatch(reply)):
        raise ConversationError("--reply-to must be a message ID returned by agent-comms send.")
    return {"kind": kind, "role": role, "display_recipient": recipient, "reply_to": reply,
            "emotion": normalize_emotion(request.get("emotion"))}


def agent(name, epoch):
    return {"kind": "agent", "name": name, "epoch": epoch}


def same(participant, other):
    if participant["kind"] != other["kind"]:
        return False
    if participant["kind"] == "user":
        return True
    return participant["name"].lower() == other["name"].lower() and participant.get("epoch") == other.get("epoch")


def resolve(request, *, sender, epoch, message_id, user_name, find_agent, find_message):
    """Return (fields, warnings) for an accepted message.

    sender: verified binding name. find_agent(name) returns a current binding name
    in this epoch or None; find_message(id) returns an accepted message or None.
    """
    fields = validate_request(request)
    speaker = agent(sender, epoch)
    warnings = []
    plain = all(request.get(key) is None for key in ("display_recipient", "role", "kind", "emotion", "reply_to"))
    if plain and not user_name:
        # Existing callers keep working without a configured user; no participant is invented.
        return {"kind": "statement", "emotion": "neutral", "speaker_role": "requester", "reply_to": None}, warnings

    def user():
        if not user_name:
            raise ConversationError("No [user].name is configured for the default display recipient; "
                                    "set user.name in config.toml or pass --display-recipient NAME.")
        return {"kind": "user", "name": user_name}

    selector = fields["display_recipient"]
    if fields["reply_to"] is not None:
        previous = find_message(fields["reply_to"])
        if previous is None:
            raise ConversationError("Unknown or expired --reply-to message ID; replies are limited to queued "
                                    "and retained history messages.")
        exchange = previous.get("exchange")
        if not isinstance(exchange, dict) or previous.get("epoch") != epoch:
            raise ConversationError("--reply-to refers to a message from before the last reset or without "
                                    "conversation metadata; start a new exchange instead.")
        sides = {"requester": exchange["requester"], "responder": exchange["responder"]}
        role = next((r for r, participant in sides.items() if same(participant, speaker)), None)
        if role is None:
            raise ConversationError("You are not a participant in the --reply-to exchange.")
        if fields["role"] is not None and fields["role"] != role:
            raise ConversationError(f"--role {fields['role']} conflicts with your {role} role in this exchange.")
        other = sides["responder" if role == "requester" else "requester"]
        if selector is not None:
            wanted = {"kind": "user"} if selector.lower() == USER_SELECTOR else {"kind": "agent", "name": selector,
                                                                                  "epoch": epoch}
            if not same(wanted, other):
                raise ConversationError("--display-recipient conflicts with the other participant in this exchange.")
        recipient = {"selector": selector, **other, "fallback": False}
        result_exchange = exchange
    else:
        role = fields["role"] or "requester"
        fallback = False
        if selector is None:
            other = user()
        elif selector.lower() == USER_SELECTOR:
            other = user()
        else:
            found = find_agent(selector)
            if found is not None and found.lower() == sender.lower():
                raise ConversationError("An exchange needs two participants; do not address yourself.")
            if found is None:
                warnings.append(f"Unknown display recipient {selector!r}; showing the configured user instead.")
                other, fallback = user(), True
            else:
                other = agent(found, epoch)
        recipient = {"selector": selector, **other, "fallback": fallback}
        result_exchange = {"id": message_id,
                           "requester": speaker if role == "requester" else other,
                           "responder": other if role == "requester" else speaker}
    return {"kind": fields["kind"] or "statement", "emotion": fields["emotion"], "speaker_role": role,
            "recipient": recipient, "exchange": result_exchange, "reply_to": fields["reply_to"]}, warnings


# --- Rendering -----------------------------------------------------------------------------

def clean(value):
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)", "", str(value))
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"\x1b.", "", value)
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C"))
    return " ".join(value.split())


def char_width(char):
    return 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1


def ink(name):
    return PALETTE[hashlib.sha256(name.encode()).digest()[0] % len(PALETTE)]


class Canvas:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.cells = [[(" ", "") for _ in range(width)] for _ in range(height)]

    def put(self, x, y, text, style=""):
        if not 0 <= y < self.height:
            return
        for char in clean(text):
            size = char_width(char)
            if size == 0:
                continue
            if 0 <= x and x + size <= self.width:
                self.cells[y][x] = (char, style)
                if size == 2:
                    self.cells[y][x + 1] = ("", style)
            x += size

    def lines(self, color):
        result = []
        for row in self.cells:
            out, current = [], ""
            for char, style in row:
                if color and style != current:
                    out.append("\x1b[0m" + (f"\x1b[{style}m" if style else ""))
                    current = style
                out.append(char)
            if color and current:
                out.append("\x1b[0m")
            result.append("".join(out))
        return result


def text_width(text):
    return sum(char_width(c) for c in clean(text))


def fit(text, width):
    text = clean(text)
    if text_width(text) <= width:
        return text
    if width <= 1:
        return "…"[:max(0, width)]
    out, used = [], 0
    for char in text:
        if used + char_width(char) > width - 1:
            break
        out.append(char)
        used += char_width(char)
    return "".join(out) + "…"


def caption_frames(text, columns, rows):
    """Group the existing caption pages into fixed-height frames on the same timeline."""
    pages = speech.pages(clean(text), max(1, columns))
    frames = []
    for start in range(0, len(pages), max(1, rows)):
        group = pages[start:start + rows]
        frames.append((tuple(line for line, _ in group), group[-1][1]))
    return frames


class ConversationView:
    """Pure renderer state; the listener timeline drives every message transition."""

    def __init__(self, *, color=True, animate=True):
        self.color, self.animate = color, animate
        self.exchange, self.entered, self.message = None, None, None
        self.offline, self.notice = True, None

    def update(self, message, now, *, offline=False):
        self.offline = offline
        if offline:
            return
        self.message = message
        exchange = (message or {}).get("exchange")
        if isinstance(exchange, dict):
            if self.exchange is None or exchange.get("id") != self.exchange.get("id"):
                self.entered = now if self.animate else None
            self.exchange = exchange
        elif message is not None:
            # Messages without metadata (older senders) still show sender -> user.
            name = clean(message.get("name", "?"))
            synthetic = {"id": message.get("uid"), "requester": {"kind": "agent", "name": name},
                         "responder": {"kind": "user", "name": "user"}}
            if self.exchange is None or self.exchange.get("id") != synthetic["id"]:
                self.entered = now if self.animate else None
            self.exchange = synthetic

    def animating(self, now):
        return self.entered is not None and now - self.entered < SLIDE_SECONDS

    def speaker_side(self):
        message = self.message
        if not message or not self.exchange:
            return None
        role = message.get("speaker_role")
        if role in ROLES:
            return role
        return "requester"

    def caption(self, columns, rows, now):
        message = self.message
        if not message:
            return ()
        if message.get("phase") != "displaying":
            return ()
        frames = caption_frames(message.get("msg", ""), columns, rows)
        started = message.get("started_at", now)
        return speech.page_at(frames, now - started, message.get("duration", 1))

    def render(self, width, height, now):
        width, height = max(1, width), max(1, height)
        if width < 44 or height < 10:
            return self.compact(width, height, now)
        canvas = Canvas(width, height)
        dim, bold = "2", "1"
        status = self.notice or ("offline" if self.offline else ("idle" if not self.message else ""))
        header = "comms · conversation" + (f" · {status}" if status else "")
        canvas.put(1, 0, fit(header, width - 2), "33" if self.offline else dim)
        message = self.message if not self.offline else None
        if message and message.get("kind") in KINDS:
            label = f"[{message['kind']}]"
            canvas.put(width - 1 - text_width(label), 0, label, "1;36")
        if not self.exchange:
            canvas.put(1, 2, "Waiting for messages…", dim)
            return canvas.lines(self.color)
        progress = 1.0
        if self.animating(now):
            t = (now - self.entered) / SLIDE_SECONDS
            progress = 1 - (1 - t) ** 3
        speaking = self.speaker_side() if message else None
        figure_width = max(12, min(24, width // 3))
        top = 2
        for side in ROLES:
            participant = self.exchange[side]
            name = fit(participant.get("name", "?"), figure_width)
            active = side == speaking
            expression = face(message.get("emotion")) if active else FACES["neutral"]
            role_label = side
            if side == "requester":
                anchor = 2
                x = round(-figure_width + (anchor + figure_width) * progress)
                align = lambda text, x=x: x
            else:
                anchor = width - 2
                right = round(width + figure_width - (width + figure_width - anchor) * progress)
                align = lambda text, right=right: right - text_width(text)
            name_style = (f"1;{ink(participant.get('name', ''))}" if active else dim)
            canvas.put(align(expression), top, expression, bold if active else dim)
            canvas.put(align(name), top + 1, name, name_style)
            canvas.put(align(role_label), top + 2, role_label, dim)
        if speaking and progress >= 1:
            arrow = "──▶" if speaking == "requester" else "◀──"
            canvas.put((width - text_width(arrow)) // 2, top + 1, arrow, "1")
        # Stable reserved text region: same height on both sides, offset toward the speaker.
        box_top = top + 4
        rows = max(1, min(6, height - box_top - 3))
        box_width = max(20, min(width - 4, int(width * 0.72)))
        left = 2 if speaking != "responder" else width - 2 - box_width
        if speaking:
            tail_x = left + 2 if speaking == "requester" else left + box_width - 3
            canvas.put(tail_x, box_top, "╲" if speaking == "requester" else "╱", dim)
        border = "2" if not speaking else ink(message.get("name", ""))
        canvas.put(left, box_top + 1, "╭" + "─" * (box_width - 2) + "╮", border)
        lines = self.caption(box_width - 4, rows, now) if speaking else ()
        if speaking and message.get("phase") != "displaying":
            lines = ("…",)
        for index in range(rows):
            text = lines[index] if index < len(lines) else ""
            canvas.put(left, box_top + 2 + index, "│", border)
            canvas.put(left + 2, box_top + 2 + index, fit(text, box_width - 4))
            canvas.put(left + box_width - 1, box_top + 2 + index, "│", border)
        canvas.put(left, box_top + 2 + rows, "╰" + "─" * (box_width - 2) + "╯", border)
        return canvas.lines(self.color)

    def compact(self, width, height, now):
        canvas = Canvas(width, height)
        message = self.message if not self.offline else None
        if self.offline:
            canvas.put(0, 0, fit(f"comms: {self.notice or 'offline'}", width), "33")
            return canvas.lines(self.color)
        if not self.exchange or not message:
            canvas.put(0, 0, fit("comms: idle", width), "2")
            if self.exchange:
                pair = f"{self.exchange['requester'].get('name', '?')} · {self.exchange['responder'].get('name', '?')}"
                if height > 1:
                    canvas.put(0, 1, fit(pair, width), "2")
            return canvas.lines(self.color)
        speaking = self.speaker_side()
        speaker = self.exchange[speaking]
        listener = self.exchange["responder" if speaking == "requester" else "requester"]
        kind = f" [{message['kind']}]" if message.get("kind") in KINDS else ""
        canvas.put(0, 0, fit(f"{speaker.get('name', '?')} ▸ {listener.get('name', '?')}{kind}", width),
                   f"1;{ink(speaker.get('name', ''))}")
        if height > 1:
            canvas.put(0, 1, fit(f"{face(message.get('emotion'))} {speaking}", width), "1")
        rows = max(1, height - 2)
        lines = self.caption(max(1, width), rows, now)
        if message.get("phase") != "displaying":
            lines = ("…",)
        for index, text in enumerate(lines[:rows]):
            canvas.put(0, 2 + index, fit(text, width))
        return canvas.lines(self.color)
