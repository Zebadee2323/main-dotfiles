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


KIND_STYLES = {"instruction": "1;30;43", "query": "1;30;46", "statement": "1;30;42"}
HEADER = "48;5;236;97"
AVATAR_WIDTH = 9


class ConversationView:
    """Pure renderer state; the listener timeline drives every message transition."""

    def __init__(self, *, color=True, animate=True):
        self.color, self.animate = color, animate
        self.exchange, self.entered, self.message = None, None, None
        self.offline, self.notice = True, None
        self.sessions, self.counts = {}, {}
        self.roles = {}

    def metadata(self, data, queued):
        self.roles = data.get("roles", {})

    def name_label(self, participant):
        name = clean(participant.get("name", "?"))
        role = self.roles.get(name.lower(), "") if participant.get("kind") == "agent" else ""
        return name + (" · " + clean(role) if role else "")

    def update(self, message, now, *, offline=False):
        self.offline = offline
        if offline:
            return
        self.message = message
        if message and isinstance(message.get("session_name"), str) and isinstance(message.get("name"), str):
            self.sessions[message["name"].lower()] = clean(message["session_name"])
        exchange = (message or {}).get("exchange")
        if not isinstance(exchange, dict) and message is not None:
            # Messages without metadata (older senders) still show sender -> user.
            exchange = {"id": message.get("uid"), "requester": {"kind": "agent", "name": clean(message.get("name", "?"))},
                        "responder": {"kind": "user", "name": "user"}}
        if isinstance(exchange, dict):
            for participant in (exchange.get("requester"), exchange.get("responder")):
                if isinstance(participant, dict) and isinstance(participant.get("session"), str):
                    self.sessions[str(participant.get("name", "")).lower()] = clean(participant["session"])
            if self.exchange is None or exchange.get("id") != self.exchange.get("id"):
                self.entered = now if self.animate else None
            self.exchange = exchange
            seen = self.counts.setdefault(exchange.get("id"), set())
            seen.add(message.get("uid"))
            if len(self.counts) > 64:
                self.counts.pop(next(iter(self.counts)))

    def animating(self, now):
        return self.entered is not None and now - self.entered < SLIDE_SECONDS

    def speaker_side(self):
        message = self.message
        if not message or not self.exchange:
            return None
        role = message.get("speaker_role")
        return role if role in ROLES else "requester"

    def session_label(self, participant):
        if participant.get("kind") == "user":
            return "human"
        session = participant.get("session") or self.sessions.get(str(participant.get("name", "")).lower())
        return session or "session unknown"

    def caption(self, columns, rows, now):
        message = self.message
        if not message or message.get("phase") != "displaying":
            return ()
        frames = caption_frames(message.get("msg", ""), columns, rows)
        started = message.get("started_at", now)
        return speech.page_at(frames, now - started, message.get("duration", 1))

    def timeline(self, now):
        message = self.message
        if not message or message.get("phase") != "displaying":
            return 0.0
        return max(0.0, min(1.0, (now - message.get("started_at", now)) / max(message.get("duration", 1), 0.01)))

    def header(self, canvas, width, message):
        canvas.put(0, 0, " " * width, HEADER)
        canvas.put(1, 0, "◉ agent-comms", f"1;{HEADER}")
        canvas.put(15, 0, "conversation", f"2;{HEADER}")
        if self.offline:
            status, style = f"✕ {self.notice or 'offline'}", f"1;33;{HEADER.split(';', 2)[0]};5;236"
        elif message:
            status, style = ("● speaking", f"1;32;48;5;236") if message.get("phase") == "displaying" \
                else ("◌ preparing", "1;33;48;5;236")
        else:
            status, style = "○ idle", f"2;{HEADER}"
        right = width - 1
        kind = message.get("kind") if message else None
        if kind in KINDS:
            chip = f" {kind.upper()} "
            right -= text_width(chip)
            canvas.put(right, 0, chip, KIND_STYLES[kind])
            right -= 2
        status = fit(status, max(0, right - 29))
        canvas.put(right - text_width(status), 0, status, style)

    def render(self, width, height, now):
        width, height = max(1, width), max(1, height)
        if width < 48 or height < 14:
            return self.compact(width, height, now)
        canvas = Canvas(width, height)
        message = self.message if not self.offline else None
        self.header(canvas, width, message)
        if not self.exchange:
            canvas.put(3, 3, "Waiting for the first message…", "2;3")
            canvas.put(3, 4, "Agents appear here as they talk; nothing is spoken by this view.", "2")
            return canvas.lines(self.color)
        progress = 1.0
        if self.animating(now):
            t = (now - self.entered) / SLIDE_SECONDS
            progress = 1 - (1 - t) ** 3
        speaking = self.speaker_side() if message else None
        label_width = max(AVATAR_WIDTH, min(24, width // 3))
        top = 2
        centers = {}
        for side in ROLES:
            participant = self.exchange[side]
            active = side == speaking
            ink_style = ink(str(participant.get("name", "")))
            expression = face(message.get("emotion")) if active else FACES["neutral"]
            name = fit(self.name_label(participant), label_width)
            session = fit("◆ " + self.session_label(participant), label_width)
            if side == "requester":
                left = round(-label_width + (2 + label_width) * progress)
                place = lambda text, left=left: left
            else:
                right = round(width + label_width - (label_width + 2) * progress)
                place = lambda text, right=right: right - text_width(text)
            box = ("╭" + "─" * (AVATAR_WIDTH - 2) + "╮", f"│ {expression} │", "╰" + "─" * (AVATAR_WIDTH - 2) + "╯")
            border = f"1;{ink_style}" if active else "2"
            for row, line in enumerate(box):
                x = place(line)
                canvas.put(x, top + row, line, border)
                if row == 1:
                    canvas.put(x + 2, top + 1, expression, "1;97" if active else "2")
            centers[side] = place(box[0]) + AVATAR_WIDTH // 2
            canvas.put(place(name), top + 3, name, f"1;{ink_style}" if active else "2;1")
            canvas.put(place(session), top + 4, session, "36" if active else "2;36")
            canvas.put(place(side), top + 5, side, "2;3")
        # Connector between the avatars: a pulse travels toward the listener while speaking.
        start, end = 2 + AVATAR_WIDTH + 2, width - 2 - AVATAR_WIDTH - 2
        if progress >= 1 and end - start >= 6:
            span = end - start
            canvas.put(start, top + 1, "─" * span, "2")
            if speaking and message.get("phase") == "displaying":
                pulse = int((now * 12) % span)
                toward_right = speaking == "requester"
                head = start + (pulse if toward_right else span - 1 - pulse)
                tail = range(head - 3, head) if toward_right else range(head + 1, head + 4)
                for x in tail:
                    if start <= x < end:
                        canvas.put(x, top + 1, "━", f"1;{ink(message.get('name', ''))}")
                canvas.put(end - 1 if toward_right else start, top + 1, "▶" if toward_right else "◀", "1")
                canvas.put(head, top + 1, "●", f"1;{ink(message.get('name', ''))}")
            elif speaking:
                dots = "·" * (1 + int(now * 3) % 3)
                canvas.put(start + (span - 3) // 2, top + 1, f"{dots:<3}", "1;33")
        # Speech bubble: stable size, shifted toward the speaker, pointer under their avatar.
        bubble_top = top + 7
        rows = max(1, min(8, height - bubble_top - 3))
        bubble_width = max(24, min(width - 4, int(width * 0.74)))
        left = 2 if speaking != "responder" else width - 2 - bubble_width
        border = ink(message.get("name", "")) if speaking else "2"
        top_line = "╭" + "─" * (bubble_width - 2) + "╮"
        if speaking and speaking in centers:
            pointer = min(max(centers[speaking] - left, 2), bubble_width - 3)
            top_line = top_line[:pointer] + "┴" + top_line[pointer + 1:]
            canvas.put(left + pointer, bubble_top - 1, "│", border)
        canvas.put(left, bubble_top, top_line, border)
        if speaking and message.get("phase") != "displaying":
            lines = ("…" * (1 + int(now * 3) % 3),)
        else:
            lines = self.caption(bubble_width - 4, rows, now) if speaking else ()
        for index in range(rows):
            text = lines[index] if index < len(lines) else ""
            canvas.put(left, bubble_top + 1 + index, "│", border)
            canvas.put(left + 2, bubble_top + 1 + index, fit(text, bubble_width - 4), "1" if speaking else "2")
            canvas.put(left + bubble_width - 1, bubble_top + 1 + index, "│", border)
        canvas.put(left, bubble_top + 1 + rows, "╰" + "─" * (bubble_width - 2) + "╯", border)
        # Footer: exchange summary and the shared timeline's progress.
        exchange_id = str(self.exchange.get("id") or "")
        count = len(self.counts.get(self.exchange.get("id"), ()))
        footer = f" exchange {exchange_id[:6]} · {count} message{'s' if count != 1 else ''}"
        if message and message.get("emotion"):
            footer += f" · {clean(message['emotion'])}"
        canvas.put(0, height - 1, fit(footer, width // 2), "2")
        if message:
            bar_width = max(8, min(24, width // 4))
            filled = round(self.timeline(now) * bar_width)
            canvas.put(width - bar_width - 2, height - 1, "▰" * filled, ink(message.get("name", "")))
            canvas.put(width - bar_width - 2 + filled, height - 1, "▱" * (bar_width - filled), "2")
        return canvas.lines(self.color)

    def compact(self, width, height, now):
        canvas = Canvas(width, height)
        message = self.message if not self.offline else None
        if self.offline:
            canvas.put(0, 0, fit(f"✕ comms: {self.notice or 'offline'}", width), "33")
            return canvas.lines(self.color)
        if not self.exchange or not message:
            canvas.put(0, 0, fit("○ comms: idle", width), "2")
            if self.exchange:
                pair = f"{self.name_label(self.exchange['requester'])} · {self.name_label(self.exchange['responder'])}"
                if height > 1:
                    canvas.put(0, 1, fit(pair, width), "2")
            return canvas.lines(self.color)
        speaking = self.speaker_side()
        speaker = self.exchange[speaking]
        listener = self.exchange["responder" if speaking == "requester" else "requester"]
        kind = message.get("kind")
        head = f"{self.name_label(speaker)} ▸ {self.name_label(listener)}"
        canvas.put(0, 0, fit(head, width), f"1;{ink(str(speaker.get('name', '')))}")
        if kind in KINDS and text_width(head) + len(kind) + 3 <= width:
            canvas.put(width - len(kind) - 2, 0, f" {kind.upper()} ", KIND_STYLES[kind])
        if height > 1:
            detail = f"{face(message.get('emotion'))} ◆ {self.session_label(speaker)}"
            canvas.put(0, 1, fit(detail, width), "36")
        rows = max(1, height - 2)
        lines = self.caption(max(1, width), rows, now)
        if message.get("phase") != "displaying":
            lines = ("…",)
        for index, text in enumerate(lines[:rows]):
            canvas.put(0, 2 + index, fit(text, width))
        return canvas.lines(self.color)
