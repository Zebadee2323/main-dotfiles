"""Static history cards and a silent log driven by queue and playback events."""
from datetime import datetime
import hashlib
import json

import conversation as c
import speech

SLIDE_SECONDS = .28


def key(message):
    return message.get('uid') or hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest()


def put(canvas, x, y, text, style=''):
    # Preserve indentation while keeping Canvas's control-sequence sanitization.
    for char in text:
        if char != ' ':
            canvas.put(x, y, char, style)
        x += c.char_width(char)


def cards(messages, width, statuses=None):
    """Return (text, style, message key) rows, preserving paragraph boundaries."""
    rows, previous_day = [], None
    for message in messages:
        uid = key(message)
        stamp = c.clean(message.get('created_at', ''))
        try:
            date = datetime.fromisoformat(stamp).astimezone()
            day, clock = date.strftime('%a %d %b %Y'), date.strftime('%H:%M:%S')
        except (ValueError, OverflowError, OSError):
            day, clock = 'Unknown date', stamp
        if day != previous_day:
            rows.append((f'── {day} ' + '─' * width, '2', None))
            previous_day = day
        name = c.clean(message.get('name', '?'))
        role = c.clean(message.get('agent_role', ''))
        recipient = message.get('recipient')
        recipient = recipient if isinstance(recipient, dict) else {}
        heading = name + (' · ' + role if role else '')
        if recipient.get('name'):
            heading += ' → ' + c.clean(recipient['name'])
        status = (statuses or {}).get(uid)
        if status:
            heading += '  [' + status + ']'
        rows.append((f'  {clock}  {heading}', f'1;{c.ink(name)}', uid))
        metadata = []
        if message.get('session_name'):
            metadata.append(c.clean(message['session_name']))
        if type(message.get('workspace')) is int:
            metadata.append(f"workspace {message['workspace']}")
        if message.get('kind') in c.KINDS:
            metadata.append(message['kind'])
        if metadata:
            rows.append(('    ' + ' · '.join(metadata), '2', uid))
        for paragraph in str(message.get('msg', '')).splitlines() or ['']:
            wrapped = speech.pages(c.clean(paragraph), max(1, width - 4))
            rows.extend(('    ' + text, '', uid) for text, _ in wrapped)
            if not wrapped:
                rows.append(('', '', uid))
        rows.append(('', '', uid))
    return rows


def document(messages, width=88, color=False):
    if not messages:
        return 'No messages in history.'
    width = max(1, min(width, 1000))
    rows = [(f"Agent communications · {len(messages)} message{'s' if len(messages) != 1 else ''}", '1;36', None)]
    rows += cards(messages, width)
    canvas = c.Canvas(width, len(rows))
    for y, (text, style, _) in enumerate(rows):
        put(canvas, 0, y, text, style)
    return '\n'.join(line.rstrip() for line in canvas.lines(color))


class LogView:
    def __init__(self, *, color=True, animate=True):
        self.color, self.animate = color, animate
        self.messages, self.entered, self.previous = [], {}, None
        self.message, self.queued, self.offline, self.notice = None, [], True, None

    def update(self, messages, now, *, snapshot=False):
        ids = [key(m) for m in messages]
        if self.animate and self.previous is not None and not snapshot:
            for uid in ids:
                if uid not in self.previous:
                    self.entered[uid] = now
        if snapshot:
            self.entered.clear()
        self.previous, self.messages = ids, messages
        self.entered = {uid: start for uid, start in self.entered.items()
                        if uid in ids and now - start < SLIDE_SECONDS}

    def receive(self, messages, response, now, *, snapshot=False):
        self.offline, self.notice = False, None
        self.message, self.queued = response.get('message'), response.get('queued', [])
        self.update(messages, now, snapshot=snapshot)

    def disconnect(self):
        self.offline, self.message, self.queued = True, None, []

    def animating(self, now):
        return any(now - start < SLIDE_SECONDS for start in self.entered.values())

    def render(self, width, height, now):
        width, height = max(1, width), max(1, height)
        canvas = c.Canvas(width, height)
        c.view_header(canvas, width, 'log', self.message, offline=self.offline, notice=self.notice)
        if height < 2:
            return canvas.lines(self.color)
        message = self.message or {}
        current = message.get('uid')
        phase = message.get('phase')
        status = '◌ preparing' if phase == 'preparing' else '▶ speaking'
        statuses = {m.get('uid'): 'queued' for m in self.queued if m.get('uid')}
        if current:
            statuses[current] = status
        detail = f'{len(self.messages)} retained · {len(self.queued)} queued'
        if current:
            detail += f" · {c.clean(message.get('name', '?'))} {status}"
            if phase == 'displaying':
                elapsed = max(0, now - message.get('started_at', now))
                progress = max(0, min(1, elapsed / max(.01, message.get('duration', 1))))
                filled = round(progress * 8)
                detail += ' ' + '▰' * filled + '▱' * (8 - filled)
        put(canvas, 0, 1, detail, '2')
        if not self.messages:
            put(canvas, 0, 2, 'Waiting for messages…' if not self.offline else 'No messages in history.', '2')
            return canvas.lines(self.color)
        rows = cards(self.messages, width, statuses)
        visible = rows[-max(1, height - 2):]
        for y, (text, style, uid) in enumerate(visible, 2):
            elapsed = now - self.entered.get(uid, now - SLIDE_SECONDS)
            t = max(0, min(1, elapsed / SLIDE_SECONDS))
            offset = round(min(16, width // 3) * (1 - t) ** 3)
            if uid == current and text.strip():
                style = '1;36' if style else '1'
            put(canvas, offset, y, text, style)
        return canvas.lines(self.color)
