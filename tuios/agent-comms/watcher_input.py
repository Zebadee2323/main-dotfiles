"""Bounded terminal input for silent watchers, with exact-pane tuios navigation."""
from contextlib import contextmanager
import json
import os
import re
import subprocess
import sys

import pane_identity
import tuios_context


class Input:
    def __init__(self, fd=None):
        self.fd, self.buffer = fd, b""

    def events(self):
        if self.fd is None:
            return []
        if os.name == 'nt':
            import msvcrt
            data = ''
            while msvcrt.kbhit() and len(data) < 4096:
                data += msvcrt.getwch()
            self.buffer = (self.buffer + data.encode('utf-8'))[-8192:]
        else:
            import select
            if select.select([self.fd], [], [], 0)[0]:
                data = os.read(self.fd, 4096)
                self.buffer = (self.buffer + data)[-8192:]
        events = []
        while self.buffer:
            match = re.match(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", self.buffer)
            if match:
                button, x, y = map(int, match.groups()[:3])
                if button == 0 and match[4] == b'M':
                    events.append(('click', x - 1, y - 1))
                self.buffer = self.buffer[match.end():]
            elif self.buffer.startswith(b'\x1b') and len(self.buffer) < 40:
                # Keep a fragmented SGR packet, discard unrelated escape keys.
                if b'\x1b[<'.startswith(self.buffer) or self.buffer.startswith(b'\x1b[<'):
                    break
                self.buffer = self.buffer[1:]
            else:
                if self.buffer[:1] in (b'q', b'Q'):
                    events.append(('queue',))
                self.buffer = self.buffer[1:]
        return events


@contextmanager
def terminal_input(*, mouse=False):
    """Keep Ctrl-C intact and restore both terminal attributes and mouse modes."""
    saved, fd, windows_console = None, None, None
    try:
        if os.name != 'nt' and sys.stdin.isatty() and sys.stdout.isatty():
            import termios
            import tty
            fd = sys.stdin.fileno()
            saved = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            if mouse:
                sys.stdout.write('\x1b[?1000h\x1b[?1006h')
                sys.stdout.flush()
        if os.name == 'nt' and sys.stdin.isatty() and sys.stdout.isatty():
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.windll.kernel32
            kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            kernel.GetStdHandle.restype = wintypes.HANDLE
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel.GetStdHandle(-10)
            mode = wintypes.DWORD()
            if kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, (mode.value & ~6) | 0x200):
                windows_console, fd = (kernel, handle, mode.value), 0
                if mouse:
                    sys.stdout.write('\x1b[?1000h\x1b[?1006h')
                    sys.stdout.flush()
        yield Input(fd)
    finally:
        if windows_console is not None:
            kernel, handle, mode = windows_console
            if mouse:
                sys.stdout.write('\x1b[?1000l\x1b[?1006l')
                sys.stdout.flush()
            kernel.SetConsoleMode(handle, mode)
        if saved is not None:
            if mouse:
                sys.stdout.write('\x1b[?1000l\x1b[?1006l')
                sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSANOW, saved)


def focus_agent(binding, *, env=None, run=subprocess.run):
    """Never infer a pane from its label, or try another route after a denial."""
    env = os.environ if env is None else env
    if env.get('TUIOS_ENV') != '1':
        raise ValueError('Navigation requires a tuios pane.')
    if not isinstance(binding, dict) or binding.get('daemon') != pane_identity.owner(env)[1]:
        raise ValueError('Agent is not bound to this tuios daemon.')
    session, pane = binding.get('session'), binding.get('pane')
    if not session or not pane:
        raise ValueError('Agent has no current tuios pane binding.')
    def command(*args):
        try:
            result = run([tuios_context.executable(env), *args], env=env, capture_output=True,
                         encoding='utf-8', timeout=3)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f'Navigation failed: {exc}') from None
        if result.returncode:
            raise ValueError((result.stderr or result.stdout or 'Navigation denied.').strip())
        return result.stdout
    listing = json.loads(command('list-windows', '-s', session, '--json'))
    if not any(w.get('window_id') == pane for w in listing.get('windows', [])):
        raise ValueError('Agent pane is no longer present.')
    command('focus-window', '-s', session, pane)
    if session != env.get('TUIOS_SESSION'):
        command('switch-session', '-s', env['TUIOS_SESSION'], session)
