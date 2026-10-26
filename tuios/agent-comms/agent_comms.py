"""Per-user agent identities, FIFO messaging, and tuios push subscriptions."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import random
import re
import secrets
import shutil
import signal
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
import unicodedata
import uuid

import message_history
import speech
import tuios_context

VERSION = 2
PREFIX_POLICY = "explicit"
MAX_FRAME = 65536
MAX_MESSAGE = 8192
QUEUE_LIMIT = 256
CLIENT_LIMIT = 128
IO_TIMEOUT = 5


class CommsError(Exception):
    """An actionable error safe to display without transport secrets."""


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise CommsError(f"Directory must not be a symlink: {path}")
    if os.name != "nt":
        if path.stat().st_uid != os.getuid():
            raise CommsError(f"Directory is not owned by this user: {path}")
        path.chmod(0o700)
    return path


def atomic_json(path: Path, value: dict) -> None:
    """Replace data, never the separate inode used for locking."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(name)


class FileLock:
    """Lock byte zero on Windows; flock on POSIX. Lock files are never removed."""

    def __init__(self, path: Path, *, wait: bool = True):
        self.path, self.wait, self.stream = path, wait, None

    def __enter__(self):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        self.stream = os.fdopen(fd, "r+b", buffering=0)
        # Windows requires a real byte to lock. All writers write the same byte.
        if not self.path.stat().st_size:
            self.stream.write(b"\0")
        deadline = time.monotonic() + IO_TIMEOUT
        try:
            while True:
                try:
                    self.stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError as exc:
                    import errno
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if not self.wait:
                        raise CommsError("A server is already running for this runtime directory.") from None
                    if time.monotonic() >= deadline:
                        raise CommsError(f"Timed out waiting for lock: {self.path}") from None
                    time.sleep(0.02)
        except BaseException:
            self.stream.close()
            raise

    def __exit__(self, *_):
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_UN)
        finally:
            self.stream.close()


@dataclass(frozen=True)
class Paths:
    config: Path
    state: Path
    runtime: Path

    @classmethod
    def resolve(cls, args):
        home = Path.home()
        config = Path(__file__).resolve().with_name("config.toml")
        if os.name == "nt":
            local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
            state = local / "agent-comms/state"
            runtime = local / "agent-comms/run"
        elif sys.platform == "darwin":
            support = home / "Library/Application Support"
            state = Path(os.environ.get("XDG_STATE_HOME", support)) / "agent-comms/state"
            runtime = Path(os.environ.get("XDG_RUNTIME_DIR", state)) / "agent-comms-run"
        else:
            state = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")) / "agent-comms"
            runtime = Path(os.environ.get("XDG_RUNTIME_DIR", state)) / "agent-comms-run"
        return cls(
            Path(args.config or os.environ.get("AGENT_COMMS_CONFIG", config)).expanduser().absolute(),
            Path(args.state_dir or os.environ.get("AGENT_COMMS_STATE_DIR", state)).expanduser().absolute(),
            Path(args.runtime_dir or os.environ.get("AGENT_COMMS_RUNTIME_DIR", runtime)).expanduser().absolute(),
        )


def clean_text(value: str, *, multiline: bool = False) -> str:
    # Strip escape sequences before controls, so their printable tails cannot leak.
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)", "", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"\x1b.", "", value)
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C") or c in "\n\t")
    return value.strip() if multiline else " ".join(value.split())


def load_config(path: Path) -> dict:
    # Config is editable source data, not private runtime state. Do not chmod its
    # parent directory or leave lock files next to it.
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except (ValueError, OSError) as exc:
        raise CommsError(f"Cannot read identity config {path}: {exc}") from None
    duration = config.get("display_seconds", 5)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 3600:
        raise CommsError("display_seconds must be a number greater than 0 and at most 3600.")
    settings = config.get("speech", {})
    if not isinstance(settings, dict) or not isinstance(settings.get("model", speech.DEFAULT_MODEL), str):
        raise CommsError("speech must be a table with a text model name.")
    speed = settings.get("speed", 1.0)
    if type(speed) not in (int, float) or not math.isfinite(speed) or not 0.7 <= speed <= 1.2:
        raise CommsError("speech.speed must be between 0.7 and 1.2.")
    identities = config.get("identities")
    if not isinstance(identities, dict) or not identities:
        raise CommsError("Config must contain at least one [identities.ID] table.")
    for identity, metadata in identities.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identity):
            raise CommsError(f"Invalid identity ID: {identity!r}")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not clean_text(metadata["name"]):
            raise CommsError(f"Identity {identity} needs a nonempty name.")
        if len(metadata["name"]) > 128:
            raise CommsError(f"Identity {identity} name is too long (maximum 128 characters).")
        for key in ("eleven_labs_voice_id", "personality"):
            if not isinstance(metadata.get(key, ""), str):
                raise CommsError(f"Identity {identity}: {key} must be text.")
    return config


def draw_id(paths: Paths, config: dict, refill: bool = False) -> str | None:
    private_dir(paths.state)
    ids = sorted(config["identities"])
    state_file = paths.state / "bag.json"
    with FileLock(paths.state / "bag.lock"):
        state = {}
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (ValueError, UnicodeError):
            print("agent-comms: corrupt bag state; starting a new cycle.", file=sys.stderr)
        remaining = state.get("remaining") if isinstance(state, dict) else None
        valid = (
            isinstance(state, dict) and state.get("ids") == ids
            and isinstance(remaining, list)
            and all(isinstance(i, str) and i in ids for i in remaining)
            and len(remaining) == len(set(remaining))
        )
        if refill or not valid or not remaining:
            remaining = ids.copy()
            random.SystemRandom().shuffle(remaining)
        identity = None if refill else remaining.pop(0)
        atomic_json(state_file, {"ids": ids, "remaining": remaining})
        return identity


def session_identity(paths: Paths, config: dict, harness: str, session_id: str) -> dict:
    """Coordinate both adapters through durable, per-session allocation locks."""
    if harness not in ("opencode", "codex") or not session_id or len(session_id) > 512:
        raise CommsError("A valid harness and nonempty session ID (at most 512 characters) are required.")
    key = hashlib.sha256(json.dumps([harness, session_id]).encode()).hexdigest()
    directory = private_dir(paths.state / "sessions")
    record = directory / f"{key}.json"
    with FileLock(directory / f"{key}.lock"):
        try:
            saved = json.loads(record.read_text(encoding="utf-8"))
        except FileNotFoundError:
            saved = None
        except (ValueError, UnicodeError):
            raise CommsError(f"Corrupt session assignment: {record}. Repair or remove it explicitly.") from None
        if saved is not None and (
            not isinstance(saved, dict) or saved.get("harness") != harness
            or saved.get("session_id") != session_id or not isinstance(saved.get("id"), str)
        ):
            raise CommsError(f"Invalid session assignment: {record}.")
        if saved is None or saved["id"] not in config["identities"]:
            saved = {"harness": harness, "session_id": session_id, "id": draw_id(paths, config)}
            atomic_json(record, saved)
    metadata = config["identities"][saved["id"]]
    template = Path(__file__).with_name("communication-instructions.md").read_text(encoding="utf-8")
    # Single-pass replacement: metadata cannot introduce more template variables.
    values = {
        "identity_name": clean_text(metadata["name"]),
        "identity_id": saved["id"],
        "personality": json.dumps(clean_text(metadata.get("personality", "")), ensure_ascii=False),
        "speech_model": config.get("speech", {}).get("model", speech.DEFAULT_MODEL),
    }
    instructions = re.sub(r"\{(identity_name|identity_id|personality|speech_model)\}", lambda m: values[m[1]], template)
    return {**metadata, **saved, "instructions": instructions}


def expected_prefix(config, identity, source):
    if identity not in config["identities"]:
        raise CommsError("Unknown identity; use an ID from the identity configuration.")
    if isinstance(source, dict) and source.get("introduce") is True and isinstance(source.get("session_name"), str):
        session = clean_text(source["session_name"])[:128]
        if session:
            return f"{clean_text(config['identities'][identity]['name'])} here, of the {session} session."
    return ""


def prefix_context(config, identity):
    if identity not in config["identities"]:
        raise CommsError("Unknown identity; use an ID from the identity configuration.")
    try:
        source = tuios_context.context(config["identities"][identity]["name"], strict=True)
    except tuios_context.ContextError as exc:
        raise CommsError(str(exc)) from None
    return expected_prefix(config, identity, source), source


def check_prefix(identity, prefix, message, tts):
    introduction = re.compile(r"^[^\n]{1,128} here, of the [^\n]{1,128} session\.")

    def after_markup(text):
        return re.sub(r"^(?:(?:<[^>\n]+>|\[[^\]\n]+\])\s*)+", "", text)

    for field, text in (("--msg", message), ("--tts", tts)):
        if prefix:
            matches = text.startswith(prefix) and (len(text) == len(prefix) or text[len(prefix)].isspace())
            remainder = text[len(prefix):].lstrip() if matches else ""
            valid = matches and bool(remainder) and not introduction.match(after_markup(remainder))
        else:
            valid = not introduction.match(after_markup(text))
        if not valid:
            wanted = json.dumps(prefix, ensure_ascii=True) if prefix else '"" (no introduction)'
            raise CommsError(
                f"Prefix mismatch in {field}. Expected prefix: {wanted}. "
                f"Run 'agent-comms prefix --id {identity}' immediately before sending; "
                "use that prefix exactly once at the start of both --msg and --tts. "
                "Focus or workspace may have changed. Message was not queued."
            )


def validate_message(config: dict, identity, message, tts=None, source=None) -> dict:
    if not isinstance(identity, str) or identity not in config["identities"]:
        raise CommsError("Unknown identity; use an ID from the identity configuration.")
    if not isinstance(message, str) or not message.strip():
        raise CommsError("Message must be nonempty text.")
    try:
        size = len(message.encode("utf-8"))
    except UnicodeEncodeError:
        raise CommsError("Message must be valid Unicode text.") from None
    if size > MAX_MESSAGE:
        raise CommsError(f"Message exceeds {MAX_MESSAGE} UTF-8 bytes.")
    message = clean_text(message, multiline=True)
    if not clean_text(message):
        raise CommsError("Message must contain printable text.")
    tts = message if tts is None else tts
    if not isinstance(tts, str) or not clean_text(tts):
        raise CommsError("TTS message must contain printable text.")
    try:
        if len(tts.encode("utf-8")) > MAX_MESSAGE:
            raise CommsError(f"TTS message exceeds {MAX_MESSAGE} UTF-8 bytes.")
    except UnicodeEncodeError:
        raise CommsError("TTS message must be valid Unicode text.") from None
    tts = clean_text(tts, multiline=True)
    check_prefix(identity, expected_prefix(config, identity, source), message, tts)
    metadata = config["identities"][identity]
    result = {"id": identity, "name": clean_text(metadata["name"]), "msg": message,
              "tts": tts, "voice_id": metadata.get("eleven_labs_voice_id", "").strip()}
    if isinstance(source, dict) and isinstance(source.get("session_name"), str):
        session = clean_text(source["session_name"])[:128]
        result["session_name"] = session
        if isinstance(source.get("pane_id"), str):
            result["pane_id"] = clean_text(source["pane_id"])[:128]
        if type(source.get("workspace")) is int:
            result["workspace"] = source["workspace"]
    if any(len(result[key].encode("utf-8")) > MAX_MESSAGE for key in ("msg", "tts")):
        raise CommsError("Message plus session introduction exceeds the message size limit.")
    return result


async def read_frame(reader: asyncio.StreamReader, *, timeout: bool = True) -> dict:
    try:
        operation = reader.readline()
        data = await asyncio.wait_for(operation, IO_TIMEOUT) if timeout else await operation
        if not data:
            raise CommsError("Listener disconnected.")
        if not data.endswith(b"\n") or len(data) > MAX_FRAME:
            raise CommsError("Invalid or oversized protocol frame.")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, asyncio.LimitOverrunError):
        raise CommsError("Invalid or oversized protocol frame.") from None


async def write_frame(writer: asyncio.StreamWriter, value: dict):
    writer.write(json.dumps(value, ensure_ascii=True).encode("utf-8") + b"\n")
    await asyncio.wait_for(writer.drain(), IO_TIMEOUT)


async def close_writer(writer):
    writer.close()
    with suppress(OSError, asyncio.TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), 1)


def endpoint(paths: Paths) -> dict:
    try:
        value = json.loads((paths.runtime / "server.json").read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        if type(value.get("protocol_version")) is not int or value["protocol_version"] != VERSION:
            raise CommsError("Unsupported discovery protocol version.")
        if type(value.get("port")) is not int or not 1 <= value["port"] <= 65535:
            raise ValueError
        if not isinstance(value.get("token"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["token"]):
            raise ValueError
        return value
    except FileNotFoundError:
        raise CommsError("Listener is offline. Start 'agent-comms serve'.") from None
    except (ValueError, UnicodeError):
        raise CommsError("Invalid discovery data. Start 'agent-comms serve' to replace it.") from None


async def connect(paths: Paths, command: str, **payload):
    info = endpoint(paths)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", info["port"], limit=MAX_FRAME), IO_TIMEOUT
        )
    except PermissionError:
        raise CommsError(
            "Localhost connection blocked by permissions or a sandbox. "
            "The listener may still be running; request approved access in your harness."
        ) from None
    except (OSError, asyncio.TimeoutError):
        raise CommsError("Listener is offline or unreachable. Start 'agent-comms serve'.") from None
    try:
        await write_frame(writer, {"protocol_version": VERSION, "token": info["token"], "command": command, **payload})
        response = await read_frame(reader)
        if response.get("ok") is not True:
            raise CommsError(clean_text(str(response.get("error", "Listener rejected the request."))))
        if response.get("protocol_version") != VERSION:
            raise CommsError("Unsupported listener protocol version.")
        return reader, writer, response
    except BaseException:
        await close_writer(writer)
        raise


async def rpc(paths: Paths, command: str, **payload):
    _, writer, response = await connect(paths, command, **payload)
    await close_writer(writer)
    return response


async def send(paths: Paths, identity: str, message: str, tts=None, source=None):
    if endpoint(paths).get("prefix_policy") != PREFIX_POLICY:
        raise CommsError("Listener predates explicit-prefix validation; restart 'agent-comms serve'. Message was not queued.")
    await rpc(paths, "send", id=identity, msg=message, tts=tts, source=source)


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def truncate(text: str, width: int) -> str:
    if cell_width(text) <= width:
        return text
    result, used = [], 0
    for char in text:
        size = cell_width(char)
        if used + size > width - 3:
            break
        result.append(char)
        used += size
    return "".join(result) + "..."


def dock_line(message: dict | None, *, color: bool = True, width: int = 96, offline: bool = False, highlight=False) -> str:
    if offline:
        text, ink = "[! ] comms: offline", "33"
    elif message is None:
        text, ink = "[o_o] comms: idle", "90"
    else:
        text = f"[o_o] {clean_text(message['name'])}: {clean_text(message['msg'])}"
        palette = ("36", "35", "32", "34", "33")
        ink = palette[hashlib.sha256(message["id"].encode()).digest()[0] % len(palette)]
    text = truncate(text, width)
    # Color the identity/prefix; leave the message itself in the dock's text ink.
    if color:
        prefix, separator, body = text.partition(":")
        line = f"\x1b[{ink}m{prefix}{separator}\x1b[0m{body}"
        return f"\x1b[7;1m{text}\x1b[0m" if highlight else line
    return text


async def watcher_speech(paths, state, settings, muted):
    message = state["message"]
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    executable = speech.player()
    if muted or not key or not message.get("voice_id") or not executable or message.get("phase") != "preparing":
        return
    audio, lease = None, None
    try:
        claim = await rpc(paths, "claim-speech", uid=message["uid"])
        if not claim.get("claimed"):
            return
        lease = claim["lease"]
        directory = private_dir(paths.state / "audio")
        cancelled, result = threading.Event(), {}

        def generate():
            generated = speech.synthesize(directory, message["voice_id"], message["tts"], key,
                                          settings.get("model", speech.DEFAULT_MODEL), settings.get("speed", 1.0))
            result["audio"] = generated
            if cancelled.is_set():
                generated.remove()
            return generated

        generation = asyncio.create_task(asyncio.to_thread(generate))
        try:
            audio = await asyncio.shield(generation)
        except asyncio.CancelledError:
            # urllib cannot be interrupted mid-request. Clean up a late result
            # without replaying it or leaving a speech file after disconnection.
            cancelled.set()
            if result.get("audio"):
                result["audio"].remove()
            def cleanup(task):
                if task.cancelled():
                    return
                with suppress(Exception):
                    task.result().remove()
            generation.add_done_callback(cleanup)
            raise
        await rpc(paths, "speech-ready", uid=message["uid"], lease=lease, duration=audio.duration)
        while state["message"].get("phase") != "displaying":
            state["changed"].clear()
            await state["changed"].wait()
        await asyncio.sleep(max(0, state["message"]["started_at"] - time.monotonic()))
        await speech.play(audio, executable)
    except (CommsError, speech.SpeechError, OSError, asyncio.TimeoutError):
        if lease:
            with suppress(CommsError, OSError, asyncio.TimeoutError):
                await rpc(paths, "speech-ready", uid=message["uid"], lease=lease, duration=None)
        print("agent-comms: speech unavailable; continuing with captions.", file=sys.stderr)
    finally:
        if audio:
            audio.remove()


async def present_message(paths, state, settings, emit, width, muted, flash):
    message = state["message"]
    display_name = truncate(message["name"], max(4, min(24, width // 3)))
    available = max(2, width - cell_width(f"[o_o] {display_name}: "))
    captions = speech.pages(clean_text(message["msg"]), available)
    voice = asyncio.create_task(watcher_speech(paths, state, settings, muted))
    try:
        while True:
            message = state["message"]
            elapsed = time.monotonic() - message.get("started_at", time.monotonic())
            text = speech.page_at(captions, elapsed, message.get("duration", 1))
            since_arrival = time.monotonic() - state["arrived"]
            highlight = flash and since_arrival < 0.6 and int(since_arrival / 0.15) % 2 == 0
            emit({**message, "name": display_name, "msg": text}, highlight=highlight)
            await asyncio.sleep(0.05)
    finally:
        voice.cancel()
        await asyncio.gather(voice, return_exceptions=True)


async def watch(paths: Paths, *, color: bool, width: int, muted=False, flash=True, settings=None):
    last, delay = None, 0.25

    def emit(message=None, offline=False, highlight=False):
        nonlocal last
        line = dock_line(message, color=color, width=width, offline=offline, highlight=highlight)
        if line != last:
            print(line, flush=True)
            last = line

    while True:
        writer, presentation, state = None, None, None
        try:
            reader, writer, response = await connect(paths, "watch")
            delay = 0.25
            while True:
                if response.get("event") != "state":
                    raise CommsError("Unexpected listener response.")
                message = response.get("message")
                if message is not None and (
                    not isinstance(message, dict)
                    or any(not isinstance(message.get(k), str) for k in ("id", "name", "msg"))
                ):
                    raise CommsError("Invalid listener display state.")
                if message is None:
                    if presentation:
                        presentation.cancel()
                        await asyncio.gather(presentation, return_exceptions=True)
                        presentation, state = None, None
                    emit()
                elif not isinstance(message.get("uid"), str) or message.get("phase") not in ("preparing", "displaying"):
                    raise CommsError("Invalid listener presentation state.")
                elif state is not None and state["message"]["uid"] == message["uid"]:
                    state["message"] = message
                    state["changed"].set()
                else:
                    if presentation:
                        presentation.cancel()
                        await asyncio.gather(presentation, return_exceptions=True)
                    state = {"message": message, "changed": asyncio.Event(), "arrived": time.monotonic()}
                    presentation = asyncio.create_task(present_message(paths, state, settings or {}, emit, width, muted, flash))
                response = await read_frame(reader, timeout=False)
        except (CommsError, OSError, asyncio.TimeoutError):
            emit(offline=True)
        finally:
            if presentation:
                presentation.cancel()
                await asyncio.gather(presentation, return_exceptions=True)
            if writer is not None:
                await close_writer(writer)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 5)


class TerminalDisplay:
    def __init__(self, quiet=False):
        self.quiet = quiet
        self.tty = sys.stdout.isatty() and not quiet

    def __enter__(self):
        if self.tty:
            print("\x1b[?1049h\x1b[?25l", end="", flush=True)
        return self

    def show(self, message):
        if self.quiet:
            return
        text = "[o_o] comms: idle" if message is None else f"{message['name']}: {message['msg']}"
        if self.tty:
            columns, rows = shutil.get_terminal_size((80, 24))
            lines = []
            for line in text.splitlines():
                lines.extend(textwrap.wrap(line, width=max(1, columns - 1), replace_whitespace=False) or [""])
            print("\x1b[H\x1b[2J" + "\n".join(lines[:max(1, rows - 1)]), end="", flush=True)
        else:
            print(text, flush=True)

    def __exit__(self, *_):
        if self.tty:
            print("\x1b[?25h\x1b[?1049l", end="", flush=True)


@contextmanager
def stop_signals(event):
    """signal.signal works on the main thread on Windows as well as POSIX."""
    loop, old = asyncio.get_running_loop(), {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(event.set))
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


class Server:
    def __init__(self, paths: Paths, config: dict, *, quiet=False):
        self.paths, self.config, self.quiet = paths, config, quiet
        self.token = secrets.token_hex(32)
        self.queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.current = None
        self.watchers = {}
        self.tasks = set()
        self.stop = asyncio.Event()
        self.claimed = asyncio.Event()
        self.ready = asyncio.Event()
        self.lease = None
        self.audio_duration = None

    def snapshot(self):
        return {"ok": True, "protocol_version": VERSION, "event": "state", "message": dict(self.current) if self.current else None}

    def publish(self, message):
        self.current = message
        self.display.show(message)
        for writer, queue in list(self.watchers.items()):
            try:
                queue.put_nowait(self.snapshot())
            except asyncio.QueueFull:
                writer.close()  # A slow subscriber must not hold up global delivery.

    async def process(self):
        while True:
            message = await self.queue.get()
            self.claimed.clear()
            self.ready.clear()
            self.lease, self.audio_duration = None, None
            self.publish({**message, "phase": "preparing"})
            # Give local watchers a short opportunity to claim speech. A single
            # lease prevents duplicate synthesis charges and overlapping players.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.claimed.wait(), 0.4)
            if self.lease:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.ready.wait(), speech.REQUEST_TIMEOUT + 10)
            duration = self.audio_duration or max(self.config.get("display_seconds", 5), speech.estimate_seconds(message["msg"]))
            delay = 0.2 if self.audio_duration else 0
            self.publish({**message, "phase": "displaying", "started_at": time.monotonic() + delay, "duration": duration})
            await asyncio.sleep(duration + delay + (0.15 if self.audio_duration else 0))
            self.queue.task_done()
            if self.queue.empty():
                self.publish(None)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        if len(self.tasks) >= CLIENT_LIMIT:
            await close_writer(writer)
            return
        self.tasks.add(task)
        try:
            request = await read_frame(reader)
            token = request.get("token")
            if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token) or not hmac.compare_digest(token, self.token):
                raise CommsError("Authentication failed.")
            if type(request.get("protocol_version")) is not int or request["protocol_version"] != VERSION:
                raise CommsError("Unsupported request protocol version.")
            if request.get("command") == "send":
                message = validate_message(self.config, request.get("id"), request.get("msg"), request.get("tts"), request.get("source"))
                if self.queue.full():
                    raise CommsError("Message queue is full; try again later.") from None
                message.update(uid=uuid.uuid4().hex, created_at=datetime.now(timezone.utc).isoformat())
                private_dir(self.paths.state)
                try:
                    with FileLock(self.paths.state / "history.lock"):
                        message_history.append(self.paths.state, message)
                except (OSError, ValueError):
                    raise CommsError("Could not save message history; message was not accepted.") from None
                self.queue.put_nowait(message)
                await write_frame(writer, {"ok": True, "protocol_version": VERSION})
            elif request.get("command") == "claim-speech":
                claimed = bool(self.current and request.get("uid") == self.current["uid"]
                               and self.current.get("phase") == "preparing" and self.lease is None)
                if claimed:
                    self.lease = secrets.token_hex(16)
                    self.claimed.set()
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "claimed": claimed,
                                           **({"lease": self.lease} if claimed else {})})
            elif request.get("command") == "speech-ready":
                if (not self.current or request.get("uid") != self.current["uid"] or not self.lease
                        or request.get("lease") != self.lease or self.current.get("phase") != "preparing" or self.ready.is_set()):
                    raise CommsError("Speech lease is no longer active.")
                duration = request.get("duration")
                if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= speech.MAX_SECONDS):
                    raise CommsError("Invalid speech duration.")
                self.audio_duration = duration
                self.ready.set()
                await write_frame(writer, {"ok": True, "protocol_version": VERSION})
            elif request.get("command") == "watch":
                updates = asyncio.Queue(maxsize=16)
                self.watchers[writer] = updates
                updates.put_nowait(self.snapshot())
                # A read task detects closed idle subscriptions without heartbeats.
                disconnected = asyncio.create_task(reader.read(1))
                try:
                    while not disconnected.done():
                        update = asyncio.create_task(updates.get())
                        try:
                            done, _ = await asyncio.wait((update, disconnected), return_when=asyncio.FIRST_COMPLETED)
                            if disconnected in done:
                                break
                            await write_frame(writer, update.result())
                        finally:
                            update.cancel()
                            await asyncio.gather(update, return_exceptions=True)
                finally:
                    disconnected.cancel()
                    await asyncio.gather(disconnected, return_exceptions=True)
            else:
                raise CommsError("Unknown command.")
        except CommsError as exc:
            with suppress(OSError, asyncio.TimeoutError):
                await write_frame(writer, {"ok": False, "error": str(exc)})
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            self.watchers.pop(writer, None)
            await close_writer(writer)
            self.tasks.discard(task)

    async def run(self):
        private_dir(self.paths.runtime)
        discovery = self.paths.runtime / "server.json"
        with FileLock(self.paths.runtime / "server.lock", wait=False):
            listener = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=MAX_FRAME)
            processor = None
            try:
                atomic_json(discovery, {"protocol_version": VERSION, "prefix_policy": PREFIX_POLICY,
                                        "port": listener.sockets[0].getsockname()[1], "token": self.token})
                with TerminalDisplay(self.quiet) as self.display, stop_signals(self.stop):
                    self.publish(None)
                    processor = asyncio.create_task(self.process())
                    # Surface unexpected worker failures instead of leaving a dead queue.
                    stopped = asyncio.create_task(self.stop.wait())
                    try:
                        done, _ = await asyncio.wait((processor, stopped), return_when=asyncio.FIRST_COMPLETED)
                        if processor in done:
                            processor.result()
                    finally:
                        stopped.cancel()
                        await asyncio.gather(stopped, return_exceptions=True)
            finally:
                listener.close()
                await listener.wait_closed()
                if processor:
                    processor.cancel()
                    await asyncio.gather(processor, return_exceptions=True)
                for task in list(self.tasks):
                    task.cancel()
                await asyncio.gather(*list(self.tasks), return_exceptions=True)
                # Still holding the singleton lock: no replacement can publish yet.
                with suppress(FileNotFoundError):
                    discovery.unlink()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    # Also accept common overrides after the subcommand, without overwriting
    # values supplied before it with subparser defaults.
    def common(target, defaults=True):
        default = None if defaults else argparse.SUPPRESS
        target.add_argument("--config", default=default, help="Identity TOML path (AGENT_COMMS_CONFIG)")
        target.add_argument("--state-dir", default=default, help="Bag state directory (AGENT_COMMS_STATE_DIR)")
        target.add_argument("--runtime-dir", default=default, help="Discovery/lock directory (AGENT_COMMS_RUNTIME_DIR)")
    common(result)
    subs = result.add_subparsers(dest="command", required=True)
    identity = subs.add_parser("id", help="Draw one identity ID")
    identity.add_argument("--refill", action="store_true", help="Reshuffle without drawing")
    serve_parser = subs.add_parser("serve", help="Run the single per-user listener")
    serve_parser.add_argument("--quiet", action="store_true", help="Disable the standalone terminal display")
    send_parser = subs.add_parser("send", help="Queue a named message")
    send_parser.add_argument("--id", required=True)
    send_parser.add_argument("--msg", required=True)
    send_parser.add_argument("--tts", help="Speech text including optional ElevenLabs tags; defaults to --msg")
    prefix = subs.add_parser("prefix", help="Print the required introduction, or an empty line when none is needed")
    prefix.add_argument("--id", required=True)
    prefix.add_argument("--json", action="store_true", help="Print the prefix and checked sender context as JSON")
    watcher = subs.add_parser("watch", help="Stream current messages as tuios push lines")
    watcher.add_argument("--no-color", action="store_true")
    watcher.add_argument("--width", type=int, default=96, help="Maximum display cells (default: 96)")
    watcher.add_argument("--mute", action="store_true", help="Display captions without generating or playing speech")
    attention = watcher.add_mutually_exclusive_group()
    attention.add_argument("--flash", dest="flash", action="store_true", help="Flash briefly for new messages (default)")
    attention.add_argument("--no-flash", dest="flash", action="store_false", help="Disable the brief attention flash for new messages")
    watcher.set_defaults(flash=True)
    history = subs.add_parser("history", help="Print previous accepted messages, even when the listener is offline")
    history.add_argument("--limit", type=int, default=message_history.MAX_ENTRIES)
    history.add_argument("--id", help="Filter by identity ID")
    history.add_argument("--session", help="Filter by tuios session name")
    history.add_argument("--json", action="store_true", help="Print full message records including speech text")
    session = subs.add_parser("session", help="Resolve a stable session identity and communication instructions")
    session.add_argument("--harness", required=True, choices=("opencode", "codex"))
    session.add_argument("--session-id", required=True)
    session.add_argument("--format", choices=("json", "text"), default="json")
    for sub in (identity, serve_parser, send_parser, prefix, watcher, session, history):
        common(sub, defaults=False)
    return result


def prepare_output():
    # Windows console argv is Unicode, but a redirected stdout pipe otherwise
    # uses the legacy code page. tuios and the protocol both expect UTF-8.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if os.name == "nt" and sys.stdout.isatty():
        # Enable SGR/alternate-screen support on the native Windows console.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle, mode = kernel.GetStdHandle(-11 & 0xFFFFFFFF), wintypes.DWORD()
        if kernel.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel.SetConsoleMode(handle, mode.value | 0x0004)


def main(argv=None):
    prepare_output()
    args = parser().parse_args(argv)
    try:
        paths = Paths.resolve(args)
        if args.command == "history":
            if not 1 <= args.limit <= message_history.MAX_ENTRIES:
                raise CommsError("--limit must be between 1 and 10.")
            messages = message_history.read(paths.state, args.limit, args.id, args.session)
            if args.json:
                print(json.dumps(messages, ensure_ascii=True))
            else:
                for message in messages:
                    print(f"{clean_text(message['created_at'])} {clean_text(message['name'])}: {clean_text(message['msg'])}")
        elif args.command == "watch":
            if not 16 <= args.width <= 1000:
                raise CommsError("--width must be between 16 and 1000.")
            try:
                settings = load_config(paths.config).get("speech", {})
            except CommsError:
                settings = {}
            asyncio.run(watch(paths, color=not args.no_color and "NO_COLOR" not in os.environ, width=args.width,
                              muted=args.mute, flash=args.flash, settings=settings))
        else:
            config = load_config(paths.config)
            if args.command == "id":
                identity = draw_id(paths, config, args.refill)
                if identity is not None:
                    print(identity)
            elif args.command == "send":
                _, source = prefix_context(config, args.id)
                validate_message(config, args.id, args.msg, args.tts, source)
                asyncio.run(send(paths, args.id, args.msg, args.tts, source))
            elif args.command == "prefix":
                introduction, source = prefix_context(config, args.id)
                print(json.dumps({"prefix": introduction, "required": bool(introduction), "context": source}, ensure_ascii=True)
                      if args.json else introduction)
            elif args.command == "serve":
                asyncio.run(Server(paths, config, quiet=args.quiet).run())
            elif args.command == "session":
                assignment = session_identity(paths, config, args.harness, args.session_id)
                print(assignment["instructions"] if args.format == "text" else json.dumps(assignment, ensure_ascii=True))
        return 0
    except BrokenPipeError:
        # A dock client may close its pipe while the watcher is printing.
        with suppress(OSError):
            sys.stdout = open(os.devnull, "w")
        return 0
    except (CommsError, OSError, asyncio.TimeoutError) as exc:
        print(f"agent-comms: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
