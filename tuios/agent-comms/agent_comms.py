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
import logging
from logging.handlers import RotatingFileHandler
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
import pane_identity
import speech
import service_manager
import tuios_context

VERSION = 2
PREFIX_POLICY = "explicit"
MAX_FRAME = 65536
MAX_MESSAGE = 8192
QUEUE_LIMIT = 256
CLIENT_LIMIT = 128
IO_TIMEOUT = 5
LOG = logging.getLogger("agent-comms")


class CommsError(Exception):
    """An actionable error safe to display without transport secrets."""


class ContextMismatch(CommsError):
    """A rejected send recoverable by recomposing with fresh context."""


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

    def __init__(self, path: Path, *, wait: bool = True, timeout: float = IO_TIMEOUT):
        self.path, self.wait, self.timeout, self.stream = path, wait, timeout, None

    def __enter__(self):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        self.stream = os.fdopen(fd, "r+b", buffering=0)
        # Windows requires a real byte to lock. All writers write the same byte.
        if not self.path.stat().st_size:
            self.stream.write(b"\0")
        deadline = time.monotonic() + self.timeout
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


class AudioWorkspace:
    """Private OS-temp audio, with locked workspaces and opportunistic orphan cleanup."""

    STALE_SECONDS = 24 * 60 * 60

    @staticmethod
    def root():
        user = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
        base = private_dir(Path(tempfile.gettempdir()) / f"agent-comms-{user}")
        return private_dir(base / "audio")

    @classmethod
    def prune(cls, root):
        # Caller holds cleanup.lock, excluding workspace creation/removal. An
        # old workspace may still be active (e.g. after sleep); age alone is not
        # sufficient. OS locks are released even on SIGKILL or process crashes.
        cutoff = time.time() - cls.STALE_SECONDS
        for path in root.glob("speech-*"):
            with suppress(OSError, CommsError):
                if path.is_symlink() or not path.is_dir() or path.stat().st_mtime >= cutoff:
                    continue
                with FileLock(path / "active.lock", wait=False):
                    pass
                shutil.rmtree(path)

    def __init__(self):
        self.root_dir = self.root()
        with FileLock(self.root_dir / "cleanup.lock"):
            self.prune(self.root_dir)
            self.path = Path(tempfile.mkdtemp(prefix="speech-", dir=self.root_dir))
            self.lock = FileLock(self.path / "active.lock")
            try:
                self.lock.__enter__()
            except BaseException:
                shutil.rmtree(self.path)
                raise

    def close(self):
        with FileLock(self.root_dir / "cleanup.lock"):
            self.lock.__exit__()
            shutil.rmtree(self.path)


def clean_text(value: str, *, multiline: bool = False) -> str:
    # Strip escape sequences before controls, so their printable tails cannot leak.
    value = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)", "", value)
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"\x1b.", "", value)
    value = "".join(c for c in value if not unicodedata.category(c).startswith("C") or c in "\n\t")
    return value.strip() if multiline else " ".join(value.split())


def format_history(messages, *, width=88, color=False):
    """Readable history; JSON and raw output deliberately bypass this renderer."""
    if not messages:
        return "No messages in history."
    width = max(24, min(width, 100))
    lines = [f"Agent communications · {len(messages)} message{'s' if len(messages) != 1 else ''}", "─" * width]
    for message in messages:
        timestamp = clean_text(message["created_at"])
        with suppress(ValueError, OverflowError, OSError):
            timestamp = datetime.fromisoformat(timestamp).astimezone().strftime("%b %d %H:%M:%S")
        name = clean_text(message["name"])
        metadata = []
        if isinstance(message.get("session_name"), str):
            metadata.append(clean_text(message["session_name"]))
        if type(message.get("workspace")) is int:
            metadata.append(f"workspace {message['workspace']}")
        heading = truncate(f"{timestamp}  {name}" + (" · " + " / ".join(metadata) if metadata else ""), width - 2)
        lines.extend(["", "  " + (f"\x1b[1;36m{heading}\x1b[0m" if color else heading)])
        for paragraph in clean_text(message["msg"], multiline=True).splitlines():
            wrapped = speech.pages(paragraph, width - 4)
            lines.extend("    " + text for text, _ in wrapped)
            if not wrapped:
                lines.append("")
    return "\n".join(lines)


def identity_tables(config):
    """Index raw TOML identity tables without losing their group or edit location."""
    legacy = config.get("identities", {})
    groups = config.get("identity_groups", {})
    if not isinstance(legacy, dict) or not isinstance(groups, dict):
        raise CommsError("identities and identity_groups must be tables.")
    identities = dict(legacy)
    for group, table in groups.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", group):
            raise CommsError(f"Invalid identity group: {group!r}")
        if not isinstance(table, dict) or not isinstance(table.get("identities"), dict) or not table["identities"]:
            raise CommsError(f"Identity group {group} needs nonempty identities.")
        for identity, metadata in table["identities"].items():
            if identity in identities:
                raise CommsError(f"Duplicate identity ID across groups: {identity}")
            identities[identity] = metadata
    return identities


def active_identity_ids(config):
    """Group activation controls new draws, not existing pane ownership."""
    if "active_groups" not in config:
        return set(config["identities"])
    return {identity for group in config["active_groups"]
            for identity in config["identity_groups"][group]["identities"]}


def voice_fields(mode):
    return {"elevenlabs": ("eleven_labs_voice_id", "voice_approved"),
            "phonon": ("phonon_voice", "phonon_voice_approved"),
             "pocket_tts": ("pocket_tts_voice", "pocket_tts_voice_approved")}[mode]


def resolve_pocket_voice(voice, config_dir):
    """Keep preset names/URLs; anchor local voice files to the config directory."""
    voice = voice.strip()
    if not voice or "://" in voice:
        return voice
    local = Path(voice).expanduser()
    if (local.is_absolute() or "/" in voice or "\\" in voice
            or local.suffix):
        return str(local if local.is_absolute() else config_dir / local)
    return voice


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
    if settings.get("mode", "elevenlabs") not in ("elevenlabs", "phonon", "pocket_tts"):
        raise CommsError("speech.mode must be 'elevenlabs', 'phonon', or 'pocket_tts'.")
    for provider, keys in (("phonon", ("model", "executable")),
                           ("pocket_tts", ("language", "executable", "python_executable"))):
        options = settings.get(provider, {})
        if not isinstance(options, dict):
            raise CommsError(f"speech.{provider} must be a table.")
        for key in keys:
            if key in options and (not isinstance(options[key], str) or not options[key].strip()):
                raise CommsError(f"speech.{provider}.{key} must be nonempty text.")
    for key in ("speed", "elevenlabs_speed", "phonon_speed", "pocket_tts_speed"):
        speed = settings.get(key, 1.0)
        if type(speed) not in (int, float) or not math.isfinite(speed) or not 0.7 <= speed <= 1.2:
            raise CommsError(f"speech.{key} must be between 0.7 and 1.2.")
    if type(settings.get("normalize", True)) is not bool:
        raise CommsError("speech.normalize must be a boolean.")
    if type(settings.get("robot_mode", False)) is not bool:
        raise CommsError("speech.robot_mode must be a boolean.")
    rejected = config.get("rejected_elevenlabs_voices", {})
    if (not isinstance(rejected, dict) or not isinstance(rejected.get("ids", []), list)
            or any(not isinstance(voice, str) or not voice.strip() for voice in rejected.get("ids", []))):
        raise CommsError("rejected_elevenlabs_voices.ids must be an array of nonempty voice ID strings.")
    identities = identity_tables(config)
    if not identities:
        raise CommsError("Config must contain at least one identity table.")
    config["identities"] = identities  # Runtime index; source remains grouped on disk.
    if "active_groups" in config:
        active = config["active_groups"]
        groups = config.get("identity_groups", {})
        if (not isinstance(active, list) or not active
                or any(not isinstance(g, str) or g not in groups for g in active)
                or len(set(active)) != len(active)):
            raise CommsError("active_groups must be a nonempty array of unique configured identity group names.")
    for identity, metadata in identities.items():
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", identity):
            raise CommsError(f"Invalid identity ID: {identity!r}")
        if not isinstance(metadata, dict) or not isinstance(metadata.get("name"), str) or not clean_text(metadata["name"]):
            raise CommsError(f"Identity {identity} needs a nonempty name.")
        if len(metadata["name"]) > 128:
            raise CommsError(f"Identity {identity} name is too long (maximum 128 characters).")
        metadata.pop("personality", None)  # Ignore obsolete fields in older user configs.
        for key in ("eleven_labs_voice_id", "phonon_voice", "pocket_tts_voice"):
            if not isinstance(metadata.get(key, ""), str):
                raise CommsError(f"Identity {identity}: {key} must be text.")
        if "pocket_tts_voice" in metadata:
            metadata["pocket_tts_voice"] = resolve_pocket_voice(
                metadata["pocket_tts_voice"], path.resolve().parent)
        for key in ("voice_approved", "phonon_voice_approved", "pocket_tts_voice_approved"):
            if type(metadata.get(key, False)) is not bool:
                raise CommsError(f"Identity {identity}: {key} must be a boolean.")
        for key in ("speed", "elevenlabs_speed", "phonon_speed", "pocket_tts_speed"):
            if key in metadata:
                speed = metadata[key]
                if type(speed) not in (int, float) or not math.isfinite(speed) or not 0.7 <= speed <= 1.2:
                    raise CommsError(f"Identity {identity}: {key} must be between 0.7 and 1.2.")
    return config


def speech_speed(settings, metadata, mode, *, legacy_key="speed"):
    """Identity overrides global; provider-specific overrides legacy at each scope."""
    key = f"{mode}_speed"
    return metadata.get(key, metadata.get(legacy_key, settings.get(key, settings.get("speed", 1.0))))


def record_rejected_voice(source: str, voice_id: str) -> str:
    """Add to the persistent avoidance list without rewriting unrelated TOML."""
    expected = tomllib.loads(source)
    rejected = expected.setdefault("rejected_elevenlabs_voices", {})
    ids = rejected.setdefault("ids", [])
    if voice_id in ids:
        return source
    ids.insert(0, voice_id)
    encoded = json.dumps(voice_id)
    # Validate each candidate structurally, so lookalike text inside multiline
    # personality strings or other tables can never be edited by accident.
    candidates = [source + f"\n[rejected_elevenlabs_voices]\nids = [{encoded}]\n"]
    candidates += [source[:m.end()] + f"{encoded}, " + source[m.end():]
                   for m in re.finditer(r"(?m)^[ \t]*ids[ \t]*=[ \t]*\[", source)]
    candidates += [source[:m.end()] + f"\nids = [{encoded}]" + source[m.end():]
                   for m in re.finditer(r"(?m)^\[rejected_elevenlabs_voices\][ \t]*(?:#[^\n]*)?$", source)]
    for candidate in candidates:
        try:
            if tomllib.loads(candidate) == expected:
                return candidate
        except ValueError:
            pass
    raise CommsError("Cannot safely edit rejected_elevenlabs_voices; use a [rejected_elevenlabs_voices] table with an ids array.")


def save_voice_approval(paths: Paths, identity: str, voice_id: str, approved: bool, *, mode="elevenlabs") -> None:
    """Save approval and rejection history together, preserving unrelated config."""
    voice_field, approval_field = voice_fields(mode)
    private_dir(paths.state)
    with FileLock(paths.state / "voice-review.lock"):
        path = paths.config.resolve()  # Keep a symlinked config intact.
        original = path.read_text(encoding="utf-8")
        expected = tomllib.loads(original)
        metadata = identity_tables(expected).get(identity)
        if metadata is None or metadata.get(voice_field, "").strip() != voice_id:
            raise CommsError(f"Identity {identity} changed during review; approval was not saved.")
        metadata[approval_field] = approved
        value = "true" if approved else "false"
        # Try surgical edits, accepting one only if TOML parsing proves that it
        # changes exactly the intended field (not text inside a personality).
        edits = [(m.start(1), m.end(1), value) for m in re.finditer(
            rf"(?m)^\s*{approval_field}\s*=\s*(true|false)\b", original)]
        edits += [(m.end(), m.end(), f"\n{approval_field} = {value}") for m in re.finditer(
            r"(?m)^\s*\[(?:identities|identity_groups\.[^\]\n]+\.identities)\.[^\n]+\][ \t]*(?:#[^\n]*)?$", original)]
        for start, end, replacement in edits:
            updated = original[:start] + replacement + original[end:]
            try:
                if tomllib.loads(updated) == expected:
                    break
            except ValueError:
                pass
        else:
            raise CommsError(f"Cannot safely edit {identity}; use an explicit identity table in the config.")
        if not approved and mode == "elevenlabs":
            updated = record_rejected_voice(updated, voice_id)
        fd, filename = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(filename, path.stat().st_mode & 0o777)
            if path.read_text(encoding="utf-8") != original:
                raise CommsError("Config changed while saving approval; try the review again.")
            os.replace(filename, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(filename)


async def voice_review_decision(audio, executable: str, name: str) -> bool | str | None:
    """Read the decision during playback, stopping audio when a decision is made."""
    playback = asyncio.create_task(speech.play(audio, executable))
    try:
        await asyncio.sleep(0)  # Start playback before an immediately available answer.
        while True:
            try:
                answer = (await asyncio.to_thread(
                    input, f"Do you like {name}'s voice? [y/n/s/r/q] (s skips, r replays) ")).strip().lower()
            except EOFError:
                return None
            if answer in ("q", "quit"):
                return None
            if answer in ("s", "skip"):
                return "skip"
            if answer in ("r", "replay"):
                playback.cancel()
                with suppress(asyncio.CancelledError):
                    await playback
                playback = asyncio.create_task(speech.play(audio, executable))
                await asyncio.sleep(0)
                continue
            if answer in ("y", "yes", "n", "no"):
                return answer in ("y", "yes")
            print("Please answer y, n, s (skip unchanged), r (replay), or q.")
    finally:
        # speech.play's cancellation cleanup terminates the player before the
        # next identity starts or its temporary audio file is removed.
        playback.cancel()
        with suppress(asyncio.CancelledError):
            await playback


def voice_review(paths: Paths, config: dict, *, all_voices=False) -> None:
    settings = config.get("speech", {})
    mode = settings.get("mode", "elevenlabs")
    voice_field, approval_field = voice_fields(mode)
    active = active_identity_ids(config)
    pending = [(identity, metadata) for identity, metadata in config["identities"].items()
               if identity in active and (all_voices or not metadata.get(approval_field, False))]
    if not pending:
        print("All identity voices are approved.")
        return
    if not sys.stdin.isatty():
        raise CommsError("voice-review requires an interactive terminal.")
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if mode == "elevenlabs" and not api_key:
        raise CommsError("Set ELEVENLABS_API_KEY before running voice-review.")
    executable = speech.player()
    if executable is None:
        raise CommsError("No audio player available; install afplay, ffplay, or aplay.")
    selection = "all" if all_voices else "non-approved"
    print(f"Reviewing {len(pending)} {selection} voices using {mode}. "
          "Decisions are saved immediately; s skips unchanged, r replays, q quits.")
    with tempfile.TemporaryDirectory(prefix="agent-comms-voice-review-") as directory:
        for index, (identity, metadata) in enumerate(pending, 1):
            name = clean_text(metadata["name"])
            voice_id = metadata.get(voice_field, "").strip()
            print(f"\n[{index}/{len(pending)}] {name} ({identity}) — {mode}: {voice_id}", flush=True)
            if not voice_id:
                print("No voice ID configured; skipping without approval.")
                continue
            text = (f"Hello, I'm {name}. Here's a quick update: the changes are ready for review. "
                    "We found one small issue, and the next step is to check the fix together. "
                    "How does this voice sound to you?")
            try:
                speed = speech_speed(settings, metadata, mode)
                if mode == "phonon":
                    audio = asyncio.run(speech.synthesize_phonon(
                        directory, voice_id, text, settings.get("phonon", {}), speed))
                elif mode == "pocket_tts":
                    audio = asyncio.run(speech.synthesize_pocket_tts(
                        directory, voice_id, text, settings.get("pocket_tts", {}), speed))
                else:
                    audio = speech.synthesize(directory, voice_id, text, api_key,
                                              settings.get("model", speech.DEFAULT_MODEL), speed)
                try:
                    approved = asyncio.run(voice_review_decision(audio, executable, name))
                finally:
                    audio.remove()
            except (speech.SpeechError, OSError, asyncio.TimeoutError) as exc:
                raise CommsError(f"Cannot audition {name}: {exc}") from None
            if approved is None:
                return
            if approved == "skip":
                print("Skipped; approval unchanged.")
                continue
            save_voice_approval(paths, identity, voice_id, approved, mode=mode)
            if approved:
                print("Approved.")
            elif mode == "elevenlabs":
                print("Not approved; voice ID added to rejected_elevenlabs_voices.ids. Will be offered again next time.")
            else:
                print("Not approved; will be offered again next time.")


def draw_id(paths: Paths, config: dict, refill: bool = False) -> str | None:
    private_dir(paths.state)
    ids = sorted(active_identity_ids(config))
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


def communication_instructions(config):
    template = Path(__file__).with_name("communication-instructions.md").read_text(encoding="utf-8")
    settings = config.get("speech", {})
    mode = settings.get("mode", "elevenlabs")
    if mode in ("phonon", "pocket_tts"):
        label = "Phonon" if mode == "phonon" else "Pocket TTS"
        guidance = (f"TTS mode: `{mode}` (local {label}). Use plain text only, without SSML "
                    "or bracketed acting tags. Use punctuation for pauses.")
    else:
        guidance = (f"TTS mode: `elevenlabs`. ElevenLabs model: `{settings.get('model', speech.DEFAULT_MODEL)}`. "
                    "For Naia-default Turbo, use natural phrasing/punctuation and occasional SSML pauses "
                    '(e.g. `<break time="0.3s" />`), never v3-only tags (`[warm]`, `[excited]`). '
                    "Plain text is valid; avoid gratuitous markup.")
    values = {
        "speech_guidance": guidance,
    }
    return re.sub(r"\{(speech_guidance)\}", lambda m: values[m[1]], template)


def pane_binding(paths, config, *, allocate=True, clear=False, env=None):
    try:
        return pane_identity.resolve(paths, config, private_dir=private_dir, lock=FileLock,
                                     atomic_json=atomic_json, draw_id=draw_id, error=CommsError, allocate=allocate,
                                     clear=clear, env=env, eligible_ids=active_identity_ids(config))
    except ValueError as exc:
        raise CommsError(str(exc)) from None


def checked_context(env=None):
    try:
        return tuios_context.context("", strict=True, **({"env": env} if env is not None else {}))
    except tuios_context.ContextError as exc:
        raise CommsError(str(exc)) from None


def identify(paths, config, env=None):
    # Verify pane presence/focus before persisting any allocation.
    source = checked_context(env) if env is not None else checked_context()
    identity = pane_binding(paths, config, **({"env": env} if env is not None else {}))
    if identity is None:
        raise CommsError("Cannot resolve identity outside a tuios pane. No caller-selected identity or session fallback is allowed.")
    prefix = expected_prefix(config, identity, source)
    name = clean_text(config["identities"][identity]["name"])
    update_pane_title(paths, config, identity, source, env)
    return {"id": identity, "name": name, "prefix": prefix}


def update_pane_title(paths, config, identity, source, env=None):
    try:
        pane_identity.update_title(paths, clean_text(config["identities"][identity]["name"]), source,
                                   private_dir=private_dir, lock=FileLock, atomic_json=atomic_json, env=env,
                                   names=[clean_text(meta["name"]) for meta in config["identities"].values()])
    except (OSError, ValueError, CommsError) as exc:
        print(f"agent-comms: pane name unchanged: {exc}", file=sys.stderr)


PANE_ENV_KEYS = frozenset(("TUIOS_ENV", "TUIOS_PANE_ID", "TUIOS_WINDOW_ID", "TUIOS_SESSION",
                           "TUIOS_HOST", "TUIOS_SOCKET", "TUIOS_SERVER"))


def request_pane_env(value):
    """Forward pane addressing only, never arbitrary process variables or credentials."""
    if (not isinstance(value, dict) or set(value) - PANE_ENV_KEYS
            or any(not isinstance(v, str) or not v or len(v) > 4096 or "\0" in v for v in value.values())):
        raise CommsError("Invalid pane environment.")
    if (value.get("TUIOS_ENV") != "1" or not (value.get("TUIOS_PANE_ID") or value.get("TUIOS_WINDOW_ID"))
            or not value.get("TUIOS_SESSION")):
        raise CommsError("Cannot resolve identity outside a tuios pane. No caller-selected identity or session fallback is allowed.")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TUIOS_")}
    return {**env, **value}


async def listener_context(paths, *, clear=False):
    env = {k: os.environ[k] for k in PANE_ENV_KEYS if k in os.environ}
    request_pane_env(env)  # Fail locally when the caller has no pane; never guess.
    try:
        source = checked_context()  # Respect local context-query denials before contacting the listener.
    except CommsError as exc:
        raise CommsError(f"Client pane check failed: {exc}") from None
    if endpoint(paths).get("pane_identity") is not True:
        raise CommsError("Listener predates sandbox-compatible identity lookup; restart 'agent-comms serve'.")
    if clear:
        if endpoint(paths).get("pane_identity_clear") is not True:
            raise CommsError("Listener predates pane identity clearing; restart 'agent-comms serve'.")
        return (await rpc(paths, "clear-context", pane_env=env))["result"]
    identity = (await rpc(paths, "context", pane_env=env))["identity"]
    # The listener owns persistent identity allocation, but may still have an
    # older prefix implementation loaded after an update. Use the same local
    # context and policy as send validation, not the listener's cached code.
    prefix = expected_prefix(load_config(paths.config), identity["id"], source)
    return {**identity, "prefix": prefix}


def expected_prefix(config, identity, source):
    if identity not in config["identities"]:
        raise CommsError("Unknown identity; use an ID from the identity configuration.")
    if isinstance(source, dict) and source.get("introduce") is True and isinstance(source.get("session_name"), str):
        session = clean_text(source["session_name"])[:128]
        if session:
            name = clean_text(config['identities'][identity]['name'])
            position = source.get("position")
            directions = {"top", "bottom", "left", "right", "top-left", "top-right", "bottom-left", "bottom-right", "center"}
            if isinstance(position, str) and position in directions:
                if source.get("position_reference") == "focused-pane" and position != "center":
                    return f"{name} here, to your {position}."
                if source.get("position_reference") == "workspace" and type(source.get("workspace")) is int:
                    preposition = "at" if position in ("top", "bottom") else "on" if position in ("left", "right") else "in"
                    workspace_name = source.get("workspace_name")
                    workspace_name = clean_text(workspace_name)[:128] if isinstance(workspace_name, str) else ""
                    workspace = f"the {workspace_name} workspace" if workspace_name else f"workspace {source['workspace']}"
                    return f"{name} here, in the {session} session, {workspace}, {preposition} the {position}."
            return f"{name} here, of the {session} session."
    return ""


def prefix_context(config, identity):
    if identity not in config["identities"]:
        raise CommsError("Unknown identity; use an ID from the identity configuration.")
    source = checked_context()
    return expected_prefix(config, identity, source), source


def check_prefix(identity, prefix, message, tts):
    direction = r"(?:top-left|top-right|bottom-left|bottom-right|top|bottom|left|right|center)"
    introduction = re.compile(
        rf"^[^\n]{{1,128}} here, (?:of the [^\n]{{1,128}} session\.|to your {direction}\."
        rf"|(?:in|at|on) the {direction} of workspace \d+, in the [^\n]{{1,128}} session\."
        rf"|in the [^\n]{{1,128}} session, (?:workspace \d+|the [^\n]{{1,128}} workspace), (?:in|at|on) the {direction}\.)")

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
            raise ContextMismatch(
                f"Prefix mismatch in {field}. Expected prefix: {wanted}. "
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
              "tts": tts, "voice_id": metadata.get("eleven_labs_voice_id", "").strip(),
              "phonon_voice": metadata.get("phonon_voice", "").strip(),
              "pocket_tts_voice": metadata.get("pocket_tts_voice", "").strip()}
    if "speed" in metadata:
        result["voice_speed"] = metadata["speed"]
    for key in ("elevenlabs_speed", "phonon_speed", "pocket_tts_speed"):
        if key in metadata:
            result[key] = metadata[key]
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
    info = endpoint(paths)
    if info.get("prefix_policy") != PREFIX_POLICY:
        raise CommsError("Listener predates explicit-prefix validation; restart 'agent-comms serve'. Message was not queued.")
    if isinstance(source, dict) and source.get("position_reference") and info.get("spatial_prefix") is not True:
        raise CommsError("Listener predates spatial prefixes; restart 'agent-comms serve'. Message was not queued.")
    pane_env = {key: os.environ[key] for key in PANE_ENV_KEYS if os.environ.get(key)}
    await rpc(paths, "send", id=identity, msg=message, tts=tts, source=source,
              **({"pane_env": pane_env} if pane_env.get("TUIOS_ENV") == "1" else {}))


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def truncate(text: str, width: int) -> str:
    if cell_width(text) <= width:
        return text
    if width <= 3:
        return "." * max(0, width)
    result, used = [], 0
    for char in text:
        size = cell_width(char)
        if used + size > width - 3:
            break
        result.append(char)
        used += size
    return "".join(result) + "..."


def delivery_prefix(text: str, frame: int, ink: str, preparing: bool) -> str:
    """Fixed-width activity glyphs and a soft, traveling identity glow (SGR only)."""
    waves = ("▁▃▅▃▁", "▂▅▇▄▂", "▄▇▅▂▃", "▆▄▂▄▆", "▄▂▄▇▄", "▂▄▇▅▂")
    orbits = ("[·  ]", "[ · ]", "[  ·]", "[ · ]")
    badge = orbits[frame % len(orbits)] if preparing else waves[frame % len(waves)]
    text = badge[:len(text)] + text[5:]
    # A narrow highlight crosses the identity; caption ink never changes.
    head = frame % (cell_width(text) + 8) - 4
    glow = ("97", "38;5;159", "38;5;117", "38;5;75")
    output, position = [], 0
    for char in text:
        distance = abs(position - head)
        style = glow[distance] if distance < len(glow) else ink
        output.append(f"\x1b[{style}m{char}")
        position += cell_width(char)
    return "".join(output) + "\x1b[0m"


def dock_line(message: dict | None, *, color: bool = True, width: int = 96, offline: bool = False, highlight=False, frame: int | None = None) -> str:
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
        if frame is not None and message is not None and not offline and not highlight:
            return delivery_prefix(prefix + separator, frame, ink, message.get("phase") == "preparing") + body
        line = f"\x1b[{ink}m{prefix}{separator}\x1b[0m{body}"
        return f"\x1b[7;1m{text}\x1b[0m" if highlight else line
    return text


async def watcher_speech(paths, state, settings, muted):
    message = state["message"]
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    executable = speech.player()
    mode = settings.get("mode", "elevenlabs")
    field = "voice_id" if mode == "elevenlabs" else voice_fields(mode)[0]
    voice = message.get(field)
    if (muted or not voice or (mode == "elevenlabs" and not key)
            or not executable or message.get("phase") != "preparing"):
        return
    audio, lease, workspace, generation = None, None, None, None
    try:
        claim = await rpc(paths, "claim-speech", uid=message["uid"])
        if not claim.get("claimed"):
            return
        lease = claim["lease"]
        workspace = AudioWorkspace()
        directory = workspace.path
        cancelled, result = threading.Event(), {}

        def generate():
            generated = speech.synthesize(directory, message["voice_id"], message["tts"], key,
                                          settings.get("model", speech.DEFAULT_MODEL),
                                          speech_speed(settings, message, mode, legacy_key="voice_speed"))
            result["audio"] = generated
            if cancelled.is_set():
                generated.remove()
            return generated

        if mode == "phonon":
            audio = await speech.synthesize_phonon(
                directory, voice, message["tts"], settings.get("phonon", {}),
                speech_speed(settings, message, mode, legacy_key="voice_speed"))
        elif mode == "pocket_tts":
            speed = speech_speed(settings, message, mode, legacy_key="voice_speed")
            worker = state.get("pocket_worker")
            if worker is not None:
                audio = await worker.synthesize(directory, voice, message["tts"], speed)
            else:
                audio = await speech.synthesize_pocket_tts(
                    directory, voice, message["tts"], settings.get("pocket_tts", {}), speed)
        else:
            generation = asyncio.create_task(asyncio.to_thread(generate))
        try:
            if generation is not None:
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
        # Effects first, then normalize their final output across voices. Each
        # optional stage falls back independently without interrupting captions.
        for enabled, process, label in (
            (settings.get("robot_mode", False), speech.robotize, "robot voice filter"),
            (settings.get("normalize", True), speech.normalize, "volume normalization"),
        ):
            if not enabled:
                continue
            try:
                processed = await process(audio)
            except (speech.SpeechError, OSError, EOFError, ValueError, asyncio.TimeoutError):
                print(f"agent-comms: {label} unavailable; keeping input audio.", file=sys.stderr)
            else:
                audio.remove()
                audio = processed
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
        if workspace:
            # A cancelled HTTP request keeps running in its worker thread. Keep
            # its workspace locked until the late result has been cleaned up.
            def close_workspace(_=None):
                with suppress(OSError, CommsError):
                    workspace.close()
            if generation is not None and not generation.done():
                generation.add_done_callback(close_workspace)
            else:
                close_workspace()


async def present_message(paths, state, settings, emit, width, muted, flash, vfx=True):
    message = state["message"]
    display_name = truncate(message["name"], max(4, min(24, width // 3)))
    available = max(2, width - cell_width(f"[o_o] {display_name}: "))
    voice = asyncio.create_task(watcher_speech(paths, state, settings, muted or state.get("audio_muted", False)))
    try:
        while True:
            if state.get("audio_muted") or state.get("speech_suppressed"):
                voice.cancel()  # Never restart this message's speech on unmute.
                await asyncio.gather(voice, return_exceptions=True)
            message = state["message"]
            captions = speech.pages(clean_text(message["msg"]), max(2, available - state.get("audio_badge_width", 0)))
            elapsed = time.monotonic() - message.get("started_at", time.monotonic())
            text = speech.page_at(captions, elapsed, message.get("duration", 1))
            since_arrival = time.monotonic() - state["arrived"]
            highlight = flash and since_arrival < 0.6 and int(since_arrival / 0.15) % 2 == 0
            frame = int(since_arrival * 10) if vfx else None
            emit({**message, "name": display_name, "msg": text}, highlight=highlight, frame=frame)
            await asyncio.sleep(0.05)
    finally:
        voice.cancel()
        await asyncio.gather(voice, return_exceptions=True)


async def watch(paths: Paths, *, color: bool, width: int, muted=False, flash=True, settings=None, vfx=True):
    worker = (speech.PocketWorker((settings or {}).get("pocket_tts", {}))
              if (settings or {}).get("mode") == "pocket_tts" else None)
    try:
        await watch_stream(paths, color=color, width=width, muted=muted, flash=flash,
                           settings=settings, vfx=vfx, pocket_worker=worker)
    finally:
        if worker is not None:
            await worker.close()


async def watch_stream(paths: Paths, *, color: bool, width: int, muted=False, flash=True,
                       settings=None, vfx=True, pocket_worker=None):
    last, delay = None, 0.25
    audio = {}

    def mute_badge():
        if not (muted or audio.get("muted")):
            return ""
        remaining = audio.get("remaining_seconds")
        return f"[Muted {remaining}s] " if not muted and remaining is not None else "[Muted] "

    def emit(message=None, offline=False, highlight=False, frame=None):
        nonlocal last
        line = dock_line(message, color=color, width=width, offline=offline, highlight=highlight, frame=frame)
        if not offline and (badge := mute_badge()):
            line = badge + dock_line(message, color=color, width=max(1, width - len(badge)),
                                     highlight=highlight, frame=frame)
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
                audio = response.get("audio", {})
                if state is not None:
                    state["audio_badge_width"] = len(mute_badge())
                    state["audio_muted"] = bool(audio.get("muted"))
                    if audio.get("muted"):
                        state["speech_suppressed"] = True
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
                    state = {"message": message, "changed": asyncio.Event(), "arrived": time.monotonic(),
                             "pocket_worker": pocket_worker,
                             "audio_muted": bool(audio.get("muted")), "speech_suppressed": bool(audio.get("muted")),
                             "audio_badge_width": len(mute_badge())}
                    presentation = asyncio.create_task(present_message(paths, state, settings or {}, emit, width, muted, flash, vfx and color))
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
    def received(sig, _):
        LOG.info("Shutdown requested: %s", signal.Signals(sig).name)
        loop.call_soon_threadsafe(event.set)
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, received)
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
        self.title_refreshes = {}
        self.stop = asyncio.Event()
        self.claimed = asyncio.Event()
        self.ready = asyncio.Event()
        self.lease = None
        self.audio_duration = None
        self.audio_muted = False
        self.muted_until = None
        self.audio_timer = None

    def refresh_sender_title(self, env, identity):
        try:
            # Never allocate or trust a submitted message's pane attribution.
            # Verify the persisted binding and the live pane in its own environment.
            if pane_binding(self.paths, self.config, allocate=False, env=env) != identity:
                return
            source = checked_context(env)
            update_pane_title(self.paths, self.config, identity, source, env)
        except (OSError, ValueError, CommsError) as exc:
            print(f"agent-comms: pane name unchanged: {exc}", file=sys.stderr)

    def schedule_title_refresh(self, identity, pane_env):
        if pane_env is None:
            return  # Older clients can still send without requesting a refresh.
        try:
            env = request_pane_env(pane_env)
            key = pane_identity.owner(env)[0]
        except (ValueError, CommsError):
            return  # Optional presentation work must not reject an accepted send.
        if key in self.title_refreshes or len(self.title_refreshes) >= CLIENT_LIMIT:
            return
        task = asyncio.create_task(asyncio.to_thread(self.refresh_sender_title, env, identity))
        self.title_refreshes[key] = task
        task.add_done_callback(lambda _: self.title_refreshes.pop(key, None))

    def audio_status(self):
        remaining = max(0, math.ceil(self.muted_until - time.monotonic())) if self.muted_until is not None else None
        return {"muted": self.audio_muted, "remaining_seconds": remaining}

    def set_audio(self, action, seconds=None):
        if action not in ("status", "toggle", "mute", "unmute"):
            raise CommsError("Unknown audio action.")
        if seconds is not None and (action != "mute" or type(seconds) not in (int, float)
                                    or not math.isfinite(seconds) or not 0 < seconds <= 86400):
            raise CommsError("--seconds must be greater than 0 and at most 86400, for mute only.")
        if action == "status":
            return self.audio_status()
        if self.audio_timer:
            self.audio_timer.cancel()
            self.audio_timer = None
        self.audio_muted = not self.audio_muted if action == "toggle" else action == "mute"
        self.muted_until = time.monotonic() + seconds if self.audio_muted and seconds is not None else None
        if self.audio_muted:
            # Release a synthesizing watcher without holding captions for its HTTP request.
            self.audio_duration = None
            self.claimed.set()
            self.ready.set()
        self.broadcast()
        if self.muted_until is not None:
            self.audio_timer = asyncio.get_running_loop().call_later(min(1, seconds), self.audio_tick)
        return self.audio_status()

    def audio_tick(self):
        self.audio_timer = None
        if time.monotonic() >= self.muted_until:
            self.set_audio("unmute")
        else:
            self.broadcast()
            self.audio_timer = asyncio.get_running_loop().call_later(
                min(1, self.muted_until - time.monotonic()), self.audio_tick)

    def snapshot(self):
        return {"ok": True, "protocol_version": VERSION, "event": "state", "message": dict(self.current) if self.current else None,
                "audio": self.audio_status()}

    def publish(self, message):
        self.current = message
        self.display.show(message)
        self.broadcast()

    def broadcast(self):
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
            if not self.audio_muted:
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
            if request.get("command") == "ping":
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "pid": os.getpid(),
                                           "service_id": os.environ.get("AGENT_COMMS_SERVICE_ID")})
            elif request.get("command") == "clear-context":
                env = request_pane_env(request.get("pane_env"))
                await asyncio.to_thread(checked_context, env)
                result = await asyncio.to_thread(pane_binding, self.paths, self.config, clear=True, env=env)
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "result": result})
            elif request.get("command") == "context":
                env = request_pane_env(request.get("pane_env"))
                # File locks and tuios queries must not stall speech/watch clients.
                try:
                    identity = await asyncio.to_thread(identify, self.paths, self.config, env)
                except (CommsError, OSError) as exc:
                    raise CommsError(f"Listener identity lookup failed: {exc}") from None
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "identity": identity})
            elif request.get("command") == "send":
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
                self.schedule_title_refresh(message["id"], request.get("pane_env"))
            elif request.get("command") == "audio":
                status = self.set_audio(request.get("action"), request.get("seconds"))
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "audio": status})
            elif request.get("command") == "claim-speech":
                claimed = bool(self.current and request.get("uid") == self.current["uid"]
                               and self.current.get("phase") == "preparing" and self.lease is None
                               and not self.audio_muted and not self.ready.is_set())
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
                atomic_json(discovery, {"protocol_version": VERSION, "prefix_policy": PREFIX_POLICY, "spatial_prefix": True,
                                        "audio_control": True, "pane_identity": True, "pane_identity_clear": True,
                                        "port": listener.sockets[0].getsockname()[1], "token": self.token})
                with TerminalDisplay(self.quiet) as self.display, stop_signals(self.stop):
                    self.publish(None)
                    LOG.info("Listener ready: pid=%s", os.getpid())
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
                if self.audio_timer:
                    self.audio_timer.cancel()
                if processor:
                    processor.cancel()
                    await asyncio.gather(processor, return_exceptions=True)
                for task in list(self.tasks):
                    task.cancel()
                await asyncio.gather(*list(self.tasks), return_exceptions=True)
                for task in list(self.title_refreshes.values()):
                    task.cancel()
                await asyncio.gather(*list(self.title_refreshes.values()), return_exceptions=True)
                # wait_closed() also waits for connected clients. Cancel their
                # handlers first so persistent watchers cannot block shutdown.
                await listener.wait_closed()
                # Still holding the singleton lock: no replacement can publish yet.
                with suppress(FileNotFoundError):
                    discovery.unlink()
                LOG.info("Listener stopped: pid=%s", os.getpid())


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
    identity = subs.add_parser("context", help="Return caller ID, name, and required message prefix as JSON")
    identity_actions = identity.add_mutually_exclusive_group()
    identity_actions.add_argument("--refill", action="store_true", help="Reshuffle without drawing")
    identity_actions.add_argument("--clear", action="store_true", help="Release the current pane's identity without allocating another")
    review = subs.add_parser("voice-review", help="Audition non-approved voices and save approval decisions")
    review.add_argument("--all", action="store_true", help="Audition all identities, including approved voices")
    serve_parser = subs.add_parser("serve", help="Run the single per-user listener")
    serve_parser.add_argument("--quiet", action="store_true", help="Disable the standalone terminal display")
    service = subs.add_parser("service", help="Manage the per-user OS service (macOS launchd / Windows Task Scheduler)")
    actions = service.add_subparsers(dest="action", required=True)
    for action in ("install", "start", "stop", "restart", "status", "logs", "uninstall"):
        target = actions.add_parser(action)
        common(target, defaults=False)
        if action == "start":
            target.add_argument("--if-installed", action="store_true", help="Skip if not installed; never install implicitly")
        if action == "logs":
            target.add_argument("--lines", type=int, default=100)
            target.add_argument("--follow", action="store_true")
    send_parser = subs.add_parser("send", help="Queue a named message")
    send_parser.add_argument("--id", required=True, help="Expected pane identity ID returned by context")
    send_parser.add_argument("--msg", required=True)
    send_parser.add_argument("--tts", help="Speech text including optional ElevenLabs tags; defaults to --msg")
    watcher = subs.add_parser("watch", help="Stream current messages as tuios push lines")
    watcher.add_argument("--no-color", action="store_true")
    watcher.add_argument("--width", type=int, default=96, help="Maximum display cells (default: 96)")
    watcher.add_argument("--mute", action="store_true", help="Display captions without generating or playing speech")
    effects = watcher.add_mutually_exclusive_group()
    effects.add_argument("--vfx", dest="vfx", action="store_true", help="Animate delivery waveform and identity glow (default)")
    effects.add_argument("--no-vfx", dest="vfx", action="store_false", help="Disable delivery animation; use --no-flash too for no motion")
    watcher.set_defaults(vfx=True)
    attention = watcher.add_mutually_exclusive_group()
    attention.add_argument("--flash", dest="flash", action="store_true", help="Flash briefly for new messages (default)")
    attention.add_argument("--no-flash", dest="flash", action="store_false", help="Disable the brief attention flash for new messages")
    watcher.set_defaults(flash=True)
    audio = subs.add_parser("audio", help="Control voice live across all watchers; captions stay enabled")
    audio.add_argument("action", choices=("status", "toggle", "mute", "unmute"))
    audio.add_argument("--seconds", type=float, help="Automatically unmute after this many seconds (mute only)")
    history = subs.add_parser("history", help="Print previous accepted messages, even when the listener is offline")
    history.add_argument("--limit", type=int, default=message_history.MAX_ENTRIES)
    history.add_argument("--id", help="Filter by identity ID")
    history.add_argument("--session", help="Filter by tuios session name")
    history_format = history.add_mutually_exclusive_group()
    history_format.add_argument("--json", action="store_true", help="Print machine-readable full records including speech text")
    history_format.add_argument("--raw", action="store_true", help="Print unstyled timestamp/name/message lines")
    history.add_argument("--no-color", action="store_true", help="Disable history heading colors")
    instructions = subs.add_parser("instructions", help="Print shared communication instructions without allocating identity")
    instructions.add_argument("--format", choices=("json", "text"), default="json")
    for sub in (identity, review, serve_parser, service, send_parser, watcher, instructions, history, audio):
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
        if args.command == "service":
            if args.action == "logs" and not 1 <= args.lines <= 10000:
                raise CommsError("--lines must be between 1 and 10000.")
            def busy():
                if not paths.runtime.exists():
                    return False
                try:
                    with FileLock(paths.runtime / "server.lock", wait=False):
                        return False
                except CommsError:
                    return True
            def probe():
                async def ping():
                    return await asyncio.wait_for(rpc(paths, "ping"), 0.5)
                try:
                    return asyncio.run(ping())
                except (CommsError, OSError, asyncio.TimeoutError):
                    return None
            def health():
                return (probe() or {}).get("pid")
            result = service_manager.manage(args, paths, private_dir=private_dir, lock=FileLock,
                                            busy=busy, health=health, probe=probe)
            if result:
                print(json.dumps(result) if isinstance(result, dict) else result)
        elif args.command == "audio":
            if args.seconds is not None and (args.action != "mute" or not math.isfinite(args.seconds) or not 0 < args.seconds <= 86400):
                raise CommsError("--seconds must be greater than 0 and at most 86400, for mute only.")
            if endpoint(paths).get("audio_control") is not True:
                raise CommsError("Listener predates live audio controls; restart 'agent-comms serve' and existing watchers once.")
            response = asyncio.run(rpc(paths, "audio", action=args.action, seconds=args.seconds))
            print(json.dumps(response["audio"]))
        elif args.command == "history":
            if not 1 <= args.limit <= message_history.MAX_ENTRIES:
                raise CommsError("--limit must be between 1 and 10.")
            messages = message_history.read(paths.state, args.limit, args.id, args.session)
            if args.json:
                print(json.dumps(messages, ensure_ascii=True))
            elif args.raw:
                for message in messages:
                    print(f"{clean_text(message['created_at'])} {clean_text(message['name'])}: {clean_text(message['msg'])}")
            else:
                print(format_history(messages, width=shutil.get_terminal_size((88, 24)).columns,
                                     color=sys.stdout.isatty() and not args.no_color and "NO_COLOR" not in os.environ))
        elif args.command == "watch":
            if not 16 <= args.width <= 1000:
                raise CommsError("--width must be between 16 and 1000.")
            try:
                settings = load_config(paths.config).get("speech", {})
            except CommsError:
                if paths.config.exists():
                    raise  # Never turn an invalid local-mode config into cloud speech.
                settings = {}
            asyncio.run(watch(paths, color=not args.no_color and "NO_COLOR" not in os.environ, width=args.width,
                              muted=args.mute, flash=args.flash, settings=settings, vfx=args.vfx))
        else:
            config = load_config(paths.config)
            if args.command == "context":
                if args.refill:
                    draw_id(paths, config, refill=True)
                elif args.clear:
                    print(json.dumps(asyncio.run(listener_context(paths, clear=True)), ensure_ascii=True, separators=(",", ":")))
                else:
                    print(json.dumps(asyncio.run(listener_context(paths)), ensure_ascii=True, separators=(",", ":")))
            elif args.command == "voice-review":
                voice_review(paths, config, all_voices=args.all)
            elif args.command == "send":
                source = checked_context()
                identity = pane_binding(paths, config, allocate=False)
                if identity is None:
                    raise CommsError("Cannot resolve sender outside a tuios pane. Run 'agent-comms context' in the caller pane before sending.")
                if args.id != identity:
                    raise ContextMismatch(
                        "Identity mismatch: --id does not match the caller pane's current identity. "
                        "Message was not queued."
                    )
                validate_message(config, identity, args.msg, args.tts, source)
                asyncio.run(send(paths, identity, args.msg, args.tts, source))
            elif args.command == "serve":
                private_dir(paths.state / "logs")
                handler = RotatingFileHandler(paths.state / "logs/server.log", maxBytes=2_000_000,
                                              backupCount=3, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                LOG.addHandler(handler)
                LOG.setLevel(logging.INFO)
                try:
                    LOG.info("Listener starting: pid=%s", os.getpid())
                    asyncio.run(Server(paths, config, quiet=args.quiet).run())
                except Exception:
                    LOG.exception("Listener failed")
                    raise
                finally:
                    LOG.removeHandler(handler)
                    handler.close()
            elif args.command == "instructions":
                assignment = {"instructions": communication_instructions(config)}
                print(assignment["instructions"] if args.format == "text" else json.dumps(assignment, ensure_ascii=True))
        return 0
    except BrokenPipeError:
        # A dock client may close its pipe while the watcher is printing.
        with suppress(OSError):
            sys.stdout = open(os.devnull, "w")
        return 0
    except ContextMismatch as exc:
        print(f"agent-comms: {exc}", file=sys.stderr)
        try:
            context = asyncio.run(listener_context(paths))
        except (CommsError, OSError, asyncio.TimeoutError) as refresh_error:
            print(f"agent-comms: Context refresh failed: {refresh_error}. "
                  "Run 'agent-comms context' before sending again.", file=sys.stderr)
        else:
            print("agent-comms: Fresh context: " + json.dumps(context, ensure_ascii=True, separators=(",", ":")),
                  file=sys.stderr)
            print("agent-comms: Use this up-to-date ID, name, and prefix; recompose both --msg and --tts "
                  "and retry once with --id. No separate context lookup is needed. "
                  "Use a nonempty prefix exactly once at the start of both arguments.", file=sys.stderr)
        return 1
    except (CommsError, service_manager.ServiceError, OSError, asyncio.TimeoutError) as exc:
        print(f"agent-comms: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
