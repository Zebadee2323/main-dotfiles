"""Generated agent names, FIFO messaging, and tuios push subscriptions."""
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
import wave

import allocation
import audio_visualizer
import conversation
import history_view
import watcher_input
import tree_view
import message_history
import pane_identity
import shell_identity
import speech
import service_manager
import tuios_context

VERSION = 3
PREFIX_POLICY = "explicit"
CAPABILITIES = {"prefix_policy": PREFIX_POLICY, "spatial_prefix": True, "audio_control": True,
                "pane_identity": True, "pane_identity_clear": True, "shell_identity": True, "queue_preview": True, "tree_navigation": True, "log_view": True,
                "name_identity": True, "conversation_view": True, "presentation_lifecycle": True, "tree_view": True}
MAX_FRAME = 65536
MAX_MESSAGE = 8192
QUEUE_LIMIT = 256
CLIENT_LIMIT = 128
IO_TIMEOUT = 5
LOG = logging.getLogger("agent-comms")
PREFIX_FIELDS = {"name", "session", "workspace", "position"}
PREFIX_RULES = {
    "focused": {"fields": [], "fallback_fields": [], "only_when_multiple_agents": False,
                "omit_single_workspace": True},
    "same_workspace": {"fields": ["name", "position"], "fallback_fields": ["name", "session"],
                       "only_when_multiple_agents": True, "omit_single_workspace": True},
    "different_workspace": {"fields": ["name", "session", "workspace", "position"],
                            "fallback_fields": ["name", "session"],
                            "only_when_multiple_agents": False, "omit_single_workspace": True},
    "different_session": {"fields": ["name", "session", "workspace", "position"],
                          "fallback_fields": ["name", "session"],
                          "only_when_multiple_agents": False, "omit_single_workspace": True},
}


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
    return history_view.document(messages, width=width, color=color)


def voice_fields(mode):
    return {"elevenlabs": ("eleven_labs_voice_id", "voice_approved"),
            "phonon": ("phonon_voice", "phonon_voice_approved"),
             "pocket_tts": ("pocket_tts_voice", "pocket_tts_voice_approved")}[mode]


def resolve_pocket_voice(voice, config_dir):
    return allocation.resolve_pocket_voice(voice, config_dir)


def load_config(path: Path) -> dict:
    # Config is editable source data, not private runtime state. Do not chmod its
    # parent directory or leave lock files next to it.
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except (ValueError, OSError) as exc:
        raise CommsError(f"Cannot read agent-comms config {path}: {exc}") from None
    user = config.get("user", {})
    if not isinstance(user, dict):
        raise CommsError("user must be a table.")
    for key in ("name", "tts_name"):
        if key in user and (not isinstance(user[key], str) or not clean_text(user[key])):
            raise CommsError(f"user.{key} must be nonempty text.")
    if "tts_name" in user and "name" not in user:
        raise CommsError("user.tts_name requires user.name.")
    prefix = config.get("prefix", {})
    if not isinstance(prefix, dict):
        raise CommsError("prefix must be a table.")
    if type(prefix.get("spatial", True)) is not bool:
        raise CommsError("prefix.spatial must be a boolean.")
    if set(prefix) - {"spatial", *PREFIX_RULES}:
        raise CommsError("Unknown prefix situation; use focused, same_workspace, different_workspace, or different_session.")
    for situation, defaults in PREFIX_RULES.items():
        rule = prefix.get(situation, {})
        if not isinstance(rule, dict) or set(rule) - set(defaults):
            raise CommsError(f"prefix.{situation} must be a table containing only {', '.join(defaults)}.")
        for key, value in rule.items():
            if key in ("fields", "fallback_fields"):
                if (not isinstance(value, list) or any(not isinstance(field, str) or field not in PREFIX_FIELDS for field in value)
                        or len(value) != len(set(value))):
                    raise CommsError(f"prefix.{situation}.{key} must be an array of unique name, session, workspace, or position fields.")
            elif type(value) is not bool:
                raise CommsError(f"prefix.{situation}.{key} must be a boolean.")
    duration = config.get("display_seconds", 5)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 3600:
        raise CommsError("display_seconds must be a number greater than 0 and at most 3600.")
    history_size = config.get("history_size", message_history.MAX_ENTRIES)
    if type(history_size) is not int or history_size < 1:
        raise CommsError("history_size must be a positive integer.")
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
    try:
        allocation.prepare_voices(config, path)
        allocation.prepare_names(config, path)
    except allocation.AllocationError as exc:
        raise CommsError(str(exc)) from None
    return config


def speech_speed(settings, metadata, mode, *, legacy_key="speed"):
    """Profile overrides global; provider-specific overrides legacy at each scope."""
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


def save_voice_approval(paths: Paths, profile: str, voice_id: str, approved: bool, *, mode="elevenlabs") -> None:
    """Save approval and rejection history together, preserving unrelated config."""
    voice_field, approval_field = voice_fields(mode)
    private_dir(paths.state)
    with FileLock(paths.state / "voice-review.lock"):
        path = paths.config.resolve()  # Keep a symlinked config intact.
        original = path.read_text(encoding="utf-8")
        expected = tomllib.loads(original)
        try:
            metadata = allocation.voice_tables(expected)[0].get(profile)
        except allocation.AllocationError as exc:
            raise CommsError(str(exc)) from None
        if metadata is None or metadata.get(voice_field, "").strip() != voice_id:
            raise CommsError(f"Voice profile {profile} changed during review; approval was not saved.")
        metadata[approval_field] = approved
        value = "true" if approved else "false"
        # Try surgical edits, accepting one only if TOML parsing proves that it
        # changes exactly the intended field and no other profile.
        edits = [(m.start(1), m.end(1), value) for m in re.finditer(
            rf"(?m)^\s*{approval_field}\s*=\s*(true|false)\b", original)]
        edits += [(m.end(), m.end(), f"\n{approval_field} = {value}") for m in re.finditer(
            r"(?m)^\s*\[voice_groups\.[^\]\n]+\.voices\.[^\n]+\][ \t]*(?:#[^\n]*)?$", original)]
        for start, end, replacement in edits:
            updated = original[:start] + replacement + original[end:]
            try:
                if tomllib.loads(updated) == expected:
                    break
            except ValueError:
                pass
        else:
            raise CommsError(f"Cannot safely edit {profile}; use an explicit voice profile table in the config.")
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
                    input, f"Do you like the {name} voice? [y/n/s/r/q] (s skips, r replays) ")).strip().lower()
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
        # next profile starts or its temporary audio file is removed.
        playback.cancel()
        with suppress(asyncio.CancelledError):
            await playback


def voice_review(paths: Paths, config: dict, *, all_voices=False) -> None:
    """Audition voice profiles in active groups. Generated agent assignments are never reviewed."""
    settings = config.get("speech", {})
    mode = settings.get("mode", "elevenlabs")
    voice_field, approval_field = voice_fields(mode)
    active = {profile for ids in allocation.eligible_profiles(config).values() for profile in ids}
    pending = [(profile, metadata) for profile, metadata in config["voices"].items()
               if profile in active and (all_voices or not metadata.get(approval_field, False))]
    if not pending:
        print("All active voice profiles are approved.")
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
        for index, (profile, metadata) in enumerate(pending, 1):
            name = profile
            voice_id = metadata.get(voice_field, "").strip()
            print(f"\n[{index}/{len(pending)}] {profile} ({metadata['gender']}) — {mode}: {voice_id}", flush=True)
            if not voice_id:
                print("No voice ID configured; skipping without approval.")
                continue
            text = ("Hello! Here's a quick update: the changes are ready for review. "
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
            save_voice_approval(paths, profile, voice_id, approved, mode=mode)
            if approved:
                print("Approved.")
            elif mode == "elevenlabs":
                print("Not approved; voice ID added to rejected_elevenlabs_voices.ids. Will be offered again next time.")
            else:
                print("Not approved; will be offered again next time.")


def communication_instructions(config):
    template = Path(__file__).with_name("communication-instructions.md").read_text(encoding="utf-8")
    user = config.get("user", {})
    if "name" in user:
        name = json.dumps(clean_text(user["name"]), ensure_ascii=False)
        tts_name = json.dumps(clean_text(user.get("tts_name", user["name"])), ensure_ascii=False)
        user_guidance = (f"The human user's name is {name} in written responses and `--msg`. "
                         f"Use {tts_name} in `--tts` when addressing or mentioning the user, "
                         "so speech pronounces their name correctly. These are two spellings "
                         "of the same user's name, not agent identities. Do not change your "
                         "checked agent name or required prefix to match the user's name.")
    else:
        user_guidance = ("No user name is configured. Do not invent one; refer to the user "
                         "generically unless they supply a name.")
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
        "user_guidance": user_guidance,
    }
    return re.sub(r"\{(speech_guidance|user_guidance)\}", lambda m: values[m[1]], template)


def source_session(source):
    if isinstance(source, dict) and isinstance(source.get("session_name"), str):
        return clean_text(source["session_name"])[:128] or None
    return None


def pane_binding(paths, config, *, allocate=True, clear=False, env=None, session=None):
    """Return (epoch, binding) for the verified owner, or a clear result.

    Without allocation, read the store lock-free: sending must never mkdir,
    chmod, or open a lock file in the listener's private state directory.
    """
    try:
        if clear:
            return pane_identity.resolve(paths, config, private_dir=private_dir, lock=FileLock,
                                         atomic_json=atomic_json, error=CommsError, clear=True, env=env)
        if not allocate:
            return pane_identity.lookup(paths, env)
        return pane_identity.resolve(paths, config, private_dir=private_dir, lock=FileLock,
                                     atomic_json=atomic_json, error=CommsError, env=env, session=session)
    except allocation.MissingBinding as exc:
        raise ContextMismatch(f"{exc} Message was not queued.") from None
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
    epoch, binding = pane_binding(paths, config, session=source_session(source),
                                  **({"env": env} if env is not None else {}))
    name = binding["name"]
    prefix = expected_prefix(config, name, source)
    update_pane_title(paths, config, name, source, env)
    # "id" is a temporary compatibility alias for name, never a second identifier.
    return {"id": name, "name": name, "epoch": epoch, "prefix": prefix}


def update_pane_title(paths, config, name, source, env=None):
    try:
        pane_identity.update_title(paths, name, source, config=config, private_dir=private_dir, lock=FileLock,
                                   atomic_json=atomic_json, env=env)
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


def request_owner_env(request):
    """Validate the standalone process owner, or retain strict tuios verification."""
    if "shell_pid" not in request:
        return request_pane_env(request.get("pane_env"))
    if request.get("pane_env"):
        raise CommsError("Cannot submit both shell and tuios ownership.")
    try:
        pid = request["shell_pid"]
        if type(pid) is not int or not 1 < pid <= 2 ** 32 - 1:
            raise ValueError("Invalid caller process ID.")
        owner = shell_identity.discover(pid)
    except ValueError as exc:
        raise CommsError(str(exc)) from None
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("TUIOS_") and k != shell_identity.ENV_KEY}
    return {**env, shell_identity.ENV_KEY: owner}


def require_capability(info, flag, message):
    if info.get(flag) is not True:
        raise CommsError(message)


async def listener_context(paths, *, clear=False):
    env = {k: os.environ[k] for k in PANE_ENV_KEYS if k in os.environ}
    standalone = env.get("TUIOS_ENV") != "1"
    if standalone:
        payload = {"shell_pid": os.getpid()}
    else:
        request_pane_env(env)
        payload = {"pane_env": env}
    try:
        source = checked_context()  # Respect local context-query denials before contacting the listener.
    except CommsError as exc:
        raise CommsError(f"Client pane check failed: {exc}") from None
    info = endpoint(paths)
    if standalone:
        require_capability(info, "shell_identity", "Listener predates standalone shell identities; restart 'agent-comms serve'.")
    require_capability(info, "pane_identity", "Listener predates sandbox-compatible identity lookup; restart 'agent-comms serve'.")
    require_capability(info, "name_identity", "Listener predates generated agent names; restart 'agent-comms serve'.")
    if clear:
        return (await rpc(paths, "clear-context", **payload))["result"]
    identity = (await rpc(paths, "context", **payload))["identity"]
    # The listener owns persistent allocation, but may still have an older
    # prefix implementation loaded after an update. Use the same local context
    # and policy as send validation, not the listener's cached code.
    prefix = expected_prefix(load_config(paths.config), identity["name"], source)
    return {"id": identity["name"], "name": identity["name"], "epoch": identity["epoch"], "prefix": prefix}


async def shell_binding(paths):
    """Read an existing shell binding through the listener, without client ps/state writes."""
    info = endpoint(paths)
    require_capability(info, "shell_identity", "Listener predates standalone shell identities; restart 'agent-comms serve'.")
    require_capability(info, "name_identity", "Listener predates generated agent names; restart 'agent-comms serve'.")
    result = await rpc(paths, "shell-binding", shell_pid=os.getpid())
    if result.get("name") is None:
        raise ContextMismatch("Shell has no current agent name. Message was not queued.")
    return result["epoch"], result["name"]


def caller_binding(paths):
    """(epoch, name) bound to this pane or shell, without allocating."""
    if os.environ.get("TUIOS_ENV") == "1":
        epoch, binding = pane_binding(paths, None, allocate=False)
        return epoch, binding["name"]
    return asyncio.run(shell_binding(paths))


def expected_prefix(config, name, source):
    if not isinstance(name, str) or not name:
        raise CommsError("Unknown agent name; run 'agent-comms context'.")
    if not isinstance(source, dict) or not isinstance(source.get("session_name"), str):
        return ""
    session = clean_text(source["session_name"])[:128]
    if not session:
        return ""
    situation = source.get("situation")
    if situation not in PREFIX_RULES:
        # Older context providers exposed only the default introduction decision.
        if source.get("introduce") is not True:
            return ""
        situation = "same_workspace" if source.get("position_reference") == "focused-pane" else "different_session"
    settings = config.get("prefix", {})
    rule = dict(PREFIX_RULES[situation])
    if settings.get("spatial", True) is False and situation != "focused":
        # Compatibility with the earlier global switch; explicit rules win.
        rule.update(fields=["name", "session"], fallback_fields=["name", "session"])
    rule.update(settings.get(situation, {}))
    if rule["only_when_multiple_agents"] and source.get("sole_agent") is True:
        return ""
    relative = situation == "same_workspace"
    position = source.get("relative_position" if relative else "absolute_position", source.get("position"))
    directions = {"top", "bottom", "left", "right", "top-left", "top-right", "bottom-left", "bottom-right", "center"}
    valid_position = isinstance(position, str) and position in directions
    if relative and position == "center":
        valid_position = False
    fields = rule["fields"]
    if "position" in fields and not valid_position:
        fields = rule["fallback_fields"]
    parts = []
    if "name" in fields:
        parts.append(clean_text(name) + " here")
    workspace = None
    if ("workspace" in fields and type(source.get("workspace")) is int
            and not (rule["omit_single_workspace"] and source.get("workspace_count") == 1
                     and type(source.get("workspace_count")) is int)):
        label = source.get("workspace_name")
        label = clean_text(label)[:128] if isinstance(label, str) else ""
        workspace = f"the {label} workspace" if label else f"workspace {source['workspace']}"
    has_position = "position" in fields and valid_position
    if "session" in fields:
        preposition = "in" if workspace or has_position else "of"
        parts.append(f"{preposition} the {session} session")
    if workspace:
        parts.append(workspace)
    if has_position:
        if relative:
            parts.append(f"to your {position}")
        else:
            preposition = "at" if position in ("top", "bottom") else "on" if position in ("left", "right") else "in"
            parts.append(f"{preposition} the {position}")
    if not parts:
        return ""
    prefix = ", ".join(parts) + "."
    return prefix if "name" in fields else prefix[0].upper() + prefix[1:]


def check_prefix(prefix, message, tts):
    direction = r"(?:top-left|top-right|bottom-left|bottom-right|top|bottom|left|right|center)"
    introduction = re.compile(
        rf"^[^\n]{{1,128}} here(?:\.|, (?:of the [^\n]{{1,128}} session\.|to your {direction}\."
        rf"|(?:in|at|on) the {direction} of workspace \d+, in the [^\n]{{1,128}} session\."
        rf"|in the [^\n]{{1,128}} session, (?:(?:workspace \d+|the [^\n]{{1,128}} workspace), )?(?:in|at|on) the {direction}\.))")
    component = (rf"(?:[^\n]{{1,128}} here|(?:in|of) the [^\n]{{1,128}} session"
                 rf"|workspace \d+|the [^\n]{{1,128}} workspace|to your {direction}|(?:in|at|on) the {direction})")
    configurable_introduction = re.compile(rf"^{component}(?:, {component}){{0,3}}\.", re.IGNORECASE)

    def after_markup(text):
        return re.sub(r"^(?:(?:<[^>\n]+>|\[[^\]\n]+\])\s*)+", "", text)

    def has_introduction(text):
        text = after_markup(text)
        return introduction.match(text) or configurable_introduction.match(text)

    for field, text in (("--msg", message), ("--tts", tts)):
        if prefix:
            matches = text.startswith(prefix) and (len(text) == len(prefix) or text[len(prefix)].isspace())
            remainder = text[len(prefix):].lstrip() if matches else ""
            valid = matches and bool(remainder) and not has_introduction(remainder)
        else:
            valid = not has_introduction(text)
        if not valid:
            wanted = json.dumps(prefix, ensure_ascii=True) if prefix else '"" (no introduction)'
            raise ContextMismatch(
                f"Prefix mismatch in {field}. Expected prefix: {wanted}. "
                "Focus or workspace may have changed. Message was not queued."
            )


def voice_settings(profile: dict) -> dict:
    """Delivery fields from an assignment's embedded voice snapshot, never live config."""
    result = {"voice_id": profile.get("eleven_labs_voice_id", "").strip(),
              "phonon_voice": profile.get("phonon_voice", "").strip(),
              "pocket_tts_voice": profile.get("pocket_tts_voice", "").strip()}
    if "speed" in profile:
        result["voice_speed"] = profile["speed"]
    for key in ("elevenlabs_speed", "phonon_speed", "pocket_tts_speed"):
        if key in profile:
            result[key] = profile[key]
    return result


def validate_message(config: dict, name, message, tts=None, source=None, profile=None) -> dict:
    if not isinstance(name, str) or not name:
        raise CommsError("Unknown agent name; run 'agent-comms context'.")
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
    check_prefix(expected_prefix(config, name, source), message, tts)
    # "id" stays equal to the generated name for older readers of history/watch frames.
    result = {"id": name, "name": clean_text(name), "msg": message, "tts": tts,
              **voice_settings(profile or {})}
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
            raise CommsError(f"Unsupported discovery protocol version: the listener speaks "
                             f"{clean_text(str(value.get('protocol_version')))}, this client needs {VERSION}. "
                             "Restart the listener and watchers with the same agent-comms version "
                             "('agent-comms restart all').")
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
            error = clean_text(str(response.get("error", "Listener rejected the request.")))
            raise (ContextMismatch if response.get("context_mismatch") is True else CommsError)(error)
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


async def send(paths: Paths, name: str, epoch: str, message: str, tts=None, source=None, display=None):
    info = endpoint(paths)
    if info.get("prefix_policy") != PREFIX_POLICY:
        raise CommsError("Listener predates explicit-prefix validation; restart 'agent-comms serve'. Message was not queued.")
    if isinstance(source, dict) and source.get("position_reference") and info.get("spatial_prefix") is not True:
        raise CommsError("Listener predates spatial prefixes; restart 'agent-comms serve'. Message was not queued.")
    require_capability(info, "name_identity", "Listener predates generated agent names; restart "
                       "'agent-comms serve'. Message was not queued.")
    pane_env = {key: os.environ[key] for key in PANE_ENV_KEYS if os.environ.get(key)}
    owner = {"pane_env": pane_env} if pane_env.get("TUIOS_ENV") == "1" else {"shell_pid": os.getpid()}
    return await rpc(paths, "send", name=name, epoch=epoch, msg=message, tts=tts, source=source,
                     **{k: v for k, v in (display or {}).items() if v is not None}, **owner)


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


def dock_line(message: dict | None, *, color: bool = True, width: int = 96, offline: bool = False, highlight=False, frame: int | None = None, mode: str = "elevenlabs", lead: str = "") -> str:
    badge = {"elevenlabs": "[ELE]", "phonon": "[PHO]", "pocket_tts": "[POC]"}[mode]
    if offline:
        text, ink = "[! ] comms: offline", "33"
    elif message is None:
        text, ink = f"{badge} comms: idle", "90"
    else:
        text = f"{badge} {clean_text(message['name'])}: {clean_text(message['msg'])}"
        palette = ("36", "35", "32", "34", "33")
        ink = palette[hashlib.sha256(message["id"].encode()).digest()[0] % len(palette)]
    text = truncate(lead + text, width)
    # Color the identity/prefix; leave the message itself in the dock's text ink.
    if color:
        prefix, separator, body = text.partition(":")
        if frame is not None and message is not None and not offline and not highlight:
            return audio_visualizer.render(
                text, width, ink, audio_visualizer.decode(message.get("visualizer")),
                message.get("visual_elapsed", -1), message.get("visual_volume", 100))
        line = f"\x1b[{ink}m{prefix}{separator}\x1b[0m{body}"
        return f"\x1b[49;1;97m{text}\x1b[0m" if highlight else line
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
        credit_cost = audio.credit_cost if mode == "elevenlabs" else None
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
        # Analyse the post-effects file once; share only its bounded envelope,
        # so every watcher follows the same speech without reading audio files.
        try:
            visualizer = await asyncio.to_thread(audio_visualizer.analyse, audio.path)
        except (OSError, EOFError, ValueError, wave.Error):
            visualizer = None
        billing = {"credit_cost": credit_cost} if type(credit_cost) in (int, float) else {}
        await rpc(paths, "speech-ready", uid=message["uid"], lease=lease, duration=audio.duration, visualizer=visualizer, **billing)
        while state["message"].get("phase") != "displaying":
            state["changed"].clear()
            await state["changed"].wait()
        await asyncio.sleep(max(0, state["message"]["started_at"] - time.monotonic()))
        await speech.play_live(audio, executable, lambda: state.get("audio_volume", 100))
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
            captions = speech.pages(clean_text(message["msg"]), max(2, available - state.get("audio_badge_width", 0)
                                                                  - state.get("credit_badge_width", lambda: 0)()))
            elapsed = time.monotonic() - message.get("started_at", time.monotonic())
            text = speech.page_at(captions, elapsed, message.get("duration", 1))
            since_arrival = time.monotonic() - state["arrived"]
            highlight = flash and since_arrival < 0.6 and int(since_arrival / 0.15) % 2 == 0
            frame = int(since_arrival * 20) if vfx else None
            visual_elapsed = elapsed if not (muted or state.get("audio_muted") or state.get("speech_suppressed")) else -1
            emit({**message, "name": display_name, "msg": text,
                  "visual_elapsed": visual_elapsed, "visual_volume": state.get("audio_volume", 100)}, highlight=highlight, frame=frame)
            await asyncio.sleep(0.05)
    finally:
        voice.cancel()
        await asyncio.gather(voice, return_exceptions=True)


def watcher_revision(paths: Paths):
    try:
        return (paths.runtime / "watcher-restart.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def restart_watchers(paths: Paths):
    private_dir(paths.runtime)
    atomic_json(paths.runtime / "watcher-restart.json", {"revision": secrets.token_hex(16)})


async def watch(paths: Paths, *, color: bool, width: int, muted=False, flash=True, settings=None, vfx=True,
                view="captions", animate=True, tree="default"):
    revision = watcher_revision(paths)
    worker = None
    if view == "log":
        stream = asyncio.create_task(watch_log(paths, color=color, animate=animate))
    elif view in ("conversation", "tree"):
        # Always silent: never claims speech, synthesizes, or plays audio.
        stream = asyncio.create_task(watch_conversation(paths, color=color, animate=animate, tree=tree if view == "tree" else None))
    else:
        worker = (speech.PocketWorker((settings or {}).get("pocket_tts", {}))
                  if (settings or {}).get("mode") == "pocket_tts" else None)
        stream = asyncio.create_task(watch_stream(
            paths, color=color, width=width, muted=muted, flash=flash,
            settings=settings, vfx=vfx, pocket_worker=worker))
    try:
        while True:
            done, _ = await asyncio.wait((stream,), timeout=0.25)
            if stream in done:
                return stream.result()
            if watcher_revision(paths) != revision:
                return True
    finally:
        stream.cancel()
        await asyncio.gather(stream, return_exceptions=True)
        if worker is not None:
            await worker.close()


async def watch_stream(paths: Paths, *, color: bool, width: int, muted=False, flash=True,
                       settings=None, vfx=True, pocket_worker=None):
    last, delay = None, 0.25
    audio = {}
    mode = (settings or {}).get("mode", "elevenlabs")
    credits, refresh, rendered = {}, asyncio.Event(), {}
    billed_uid = None
    volume_timer = None

    def volume_badge():
        if audio.get("volume_notice_until", 0) > time.monotonic():
            return f"[Volume {audio.get('volume', 100)}%] "
        return ""

    def credit_badge(message):
        if mode != "elevenlabs":
            return ""
        balance = credits.get("remaining")
        left = f"{balance:,.10g}" if balance is not None else "?"
        cost = (message or {}).get("credit_cost")
        charge = f" | {cost:,.10g}" if type(cost) in (int, float) else ""
        return f"[{left}{charge}] "

    async def poll_credits():
        key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if mode != "elevenlabs" or not key:
            return
        while True:
            refresh.clear()
            try:
                credits["remaining"] = await asyncio.to_thread(speech.remaining_credits, key)
            except speech.SpeechError:
                credits.pop("remaining", None)
            if rendered:
                emit(**rendered)
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(refresh.wait(), 60)

    def mute_badge():
        if not (muted or audio.get("muted")):
            return ""
        remaining = audio.get("remaining_seconds")
        return f"[Muted {remaining}s] " if not muted and remaining is not None else "[Muted] "

    def emit(message=None, offline=False, highlight=False, frame=None):
        nonlocal last
        rendered.update(message=message, offline=offline, highlight=highlight, frame=frame)
        badge = "" if offline else volume_badge() + mute_badge() + credit_badge(message)
        # Reserve caption space for billing; truncate even on very narrow docks.
        badge = truncate(badge, max(0, width - 12)) if badge else ""
        line = dock_line(message, color=color, width=width, lead=badge,
                         offline=offline, highlight=highlight, frame=frame, mode=mode)
        if line != last:
            print(line, flush=True)
            last = line

    billing_task = asyncio.create_task(poll_credits())
    try:
        while True:
            writer, presentation, state = None, None, None
            try:
                reader, writer, response = await connect(paths, "watch")
                delay = 0.25
                while True:
                    if response.get("event") != "state":
                        raise CommsError("Unexpected listener response.")
                    audio = response.get("audio", {})
                    if volume_timer is not None:
                        volume_timer.cancel()
                    notice_remaining = audio.get("volume_notice_until", 0) - time.monotonic()
                    if notice_remaining > 0:
                        volume_timer = asyncio.get_running_loop().call_later(
                            notice_remaining, lambda: emit(**rendered))
                    if state is not None:
                        state["audio_volume"] = audio.get("volume", 100)
                        state["audio_badge_width"] = len(mute_badge())
                        state["audio_muted"] = bool(audio.get("muted"))
                        if audio.get("muted"):
                            state["speech_suppressed"] = True
                    message = response.get("message")
                    if (mode == "elevenlabs" and isinstance(message, dict) and "credit_cost" in message
                            and message.get("uid") != billed_uid):
                        billed_uid = message.get("uid")
                        refresh.set()
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
                                 "audio_volume": audio.get("volume", 100),
                                 "audio_muted": bool(audio.get("muted")), "speech_suppressed": bool(audio.get("muted")),
                                 "audio_badge_width": len(mute_badge()),
                                 "credit_badge_width": lambda: cell_width(volume_badge() + credit_badge(state["message"]))}
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
    finally:
        if volume_timer is not None:
            volume_timer.cancel()
        billing_task.cancel()
        await asyncio.gather(billing_task, return_exceptions=True)


@contextmanager
def terminal_screen():
    """Alternate screen for full-pane views; leaves the user's scrollback untouched."""
    tty = sys.stdout.isatty()
    if tty:
        print("\x1b[?1049h\x1b[?25l", end="", flush=True)
    try:
        yield tty
    finally:
        if tty:
            print("\x1b[0m\x1b[?25h\x1b[?1049l", end="", flush=True)


class ScreenWriter:
    """Redraw in place only when the content or terminal size changes."""

    def __init__(self, stream=None):
        self.stream, self.last = stream, None

    def draw(self, lines, size):
        key = (tuple(lines), tuple(size))
        if key == self.last:
            return False
        self.last = key
        stream = self.stream or sys.stdout
        stream.write("\x1b[H" + "\r\n".join(line + "\x1b[K" for line in lines) + "\x1b[J")
        stream.flush()
        return True


def valid_display_message(message):
    return (isinstance(message, dict) and all(isinstance(message.get(k), str) for k in ("id", "name", "msg", "uid"))
            and message.get("phase") in ("preparing", "displaying"))


async def watch_log(paths: Paths, *, color=True, animate=True, size=None, writer=None):
    return await watch_conversation(paths, color=color, animate=animate, size=size, writer=writer, log=True)


async def watch_conversation(paths: Paths, *, color=True, animate=True, size=None, writer=None, tree=None, log=False):
    """Silent pane watchers driven by the listener's shared queue and lifecycle."""
    if log:
        view = history_view.LogView(color=color, animate=animate)
        config = load_config(paths.config) if paths.config.exists() else {}
        history_size = config.get("history_size", message_history.MAX_ENTRIES)
        view.update(await asyncio.to_thread(message_history.read, paths.state, history_size,
                                            max_entries=history_size), time.monotonic(), snapshot=True)
    else:
        view = tree_view.TreeView(tree, color=color) if tree is not None else conversation.ConversationView(color=color, animate=animate)
    screen = ScreenWriter(writer)
    changed = asyncio.Event()
    seen = None

    async def subscribe():
        nonlocal seen
        delay = 0.25
        while True:
            connection = None
            try:
                info = endpoint(paths)
                if (info.get("presentation_lifecycle") is not True
                        or (log and info.get("log_view") is not True)
                        or (not log and tree is not None and info.get("tree_navigation") is not True)
                        or (not log and tree is None and info.get("queue_preview") is not True)):
                    view.notice = "listener too old; run 'agent-comms restart all'"
                    raise CommsError("Listener predates the conversation view.")
                reader, connection, response = await connect(paths, "watch", presentation=True)
                view.notice, delay = None, 0.25
                snapshot = True
                while True:
                    lifecycle = response.get("lifecycle")
                    if response.get("event") != "state" or not isinstance(lifecycle, dict):
                        raise CommsError("Unexpected listener response.")
                    if log:
                        messages = await asyncio.to_thread(message_history.read, paths.state, history_size,
                                                           max_entries=history_size)
                        view.receive(messages, response, time.monotonic(), snapshot=snapshot)
                        snapshot = False
                        changed.set()
                        response = await read_frame(reader, timeout=False)
                        continue
                    view.metadata(response.get("presentation", {}), response.get("queued", []), now=time.monotonic())
                    changed.set()
                    position = (lifecycle.get("timeline"), lifecycle.get("seq"))
                    # Deduplicate by lifecycle position; a new timeline resynchronizes.
                    if seen is None or position[0] != seen[0] or (type(position[1]) is int and position[1] > seen[1]):
                        seen = position
                        message = response.get("message")
                        view.update(message if valid_display_message(message) else None, time.monotonic())
                        changed.set()
                    response = await read_frame(reader, timeout=False)
            except (CommsError, OSError, asyncio.TimeoutError):
                if log:
                    view.disconnect()
                else:
                    view.update(None, time.monotonic(), offline=True)
                changed.set()
            finally:
                if connection is not None:
                    await close_writer(connection)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5)

    task = asyncio.create_task(subscribe())
    try:
        with terminal_screen(), watcher_input.terminal_input(mouse=tree is not None) as inputs:
            while True:
                for event in inputs.events():
                    if event[0] == "queue" and tree is None and not log:
                        view.expanded_queue = not view.expanded_queue
                    elif event[0] == "click" and tree is not None:
                        binding = view.hit_test(event[1], event[2])
                        if binding:
                            try:
                                await asyncio.to_thread(watcher_input.focus_agent, binding)
                                view.notice = None
                            except (ValueError, OSError) as exc:
                                view.notice = clean_text(str(exc))[:180]
                columns, rows = size() if size else shutil.get_terminal_size((80, 24))
                screen.draw(view.render(max(1, columns - 1), max(1, rows), time.monotonic()), (columns, rows))
                if task.done():
                    task.result()
                changed.clear()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(changed.wait(), 0.05)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)



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
        self.generation = secrets.token_hex(16)  # Public restart identity, not the auth token.
        self.queue = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.current = None
        self.pending = {}
        self.preparing_started_at = None
        self.watchers = {}
        self.presentation_watchers = set()
        self.presentation = {}
        self.presentation_stamp = None
        self.tasks = set()
        self.title_refreshes = {}
        self.stop = asyncio.Event()
        self.claimed = asyncio.Event()
        self.ready = asyncio.Event()
        self.lease = None
        self.audio_duration = None
        self.audio_muted = False
        self.audio_volume = 100
        self.volume_notice_until = 0
        self.muted_until = None
        self.audio_timer = None
        self.accepting = 0
        # One authoritative presentation timeline shared by every subscriber.
        self.lifecycle = {"timeline": self.generation, "seq": 0, "event": "idle", "message_id": None}

    def refresh_sender_title(self, env, name):
        try:
            # Never allocate or trust a submitted message's pane attribution.
            # Verify the persisted binding and the live pane in its own environment.
            if pane_binding(self.paths, self.config, allocate=False, env=env)[1]["name"] != name:
                return
            source = checked_context(env)
            update_pane_title(self.paths, self.config, name, source, env)
        except (OSError, ValueError, CommsError) as exc:
            print(f"agent-comms: pane name unchanged: {exc}", file=sys.stderr)

    def find_message(self, uid):
        """Accepted messages still queued, presenting, or retained in history."""
        if uid in self.pending:
            return self.pending[uid]
        if self.current and self.current.get("uid") == uid:
            return self.current
        records = message_history.read(self.paths.state, max_entries=self.config.get(
            "history_size", message_history.MAX_ENTRIES))
        return next((record for record in records if record.get("uid") == uid), None)

    def accept(self, request, env):
        """Verify the owner's current binding and record the message in one serialized step."""
        key = pane_identity.owner(env)[0]
        store = pane_identity.store(self.paths, lock=FileLock, atomic_json=atomic_json, private_dir=private_dir)
        user = self.config.get("user", {}).get("name")

        def operation(state):
            binding = (state or {}).get("bindings", {}).get(key)
            if binding is None:
                raise ContextMismatch("Caller has no current agent name. Message was not queued.")
            if request.get("name") != binding["name"] or request.get("epoch") != state["epoch"]:
                raise ContextMismatch("Name/epoch mismatch: --name and --epoch must match the caller pane or "
                                      "shell's current assignment (contexts from before a reset are invalid). "
                                      "Message was not queued.")
            message = validate_message(self.config, binding["name"], request.get("msg"), request.get("tts"),
                                       request.get("source"), binding["voice_profile"])
            uid = uuid.uuid4().hex
            try:
                fields, warnings = conversation.resolve(
                    request, sender=binding["name"], epoch=state["epoch"], message_id=uid,
                    user_name=clean_text(user) if isinstance(user, str) else None,
                    find_agent=lambda name: (allocation.find_agent(state, name) or {}).get("name"),
                    find_message=self.find_message)
            except conversation.ConversationError as exc:
                raise CommsError(f"{exc} Message was not queued.") from None
            message["agent_role"] = binding.get("agent_role", "")
            self.presentation = tree_view.presentation(state)
            message.update(uid=uid, message_id=uid, created_at=datetime.now(timezone.utc).isoformat(),
                           epoch=state["epoch"], voice_profile_id=binding["voice_profile_id"], **fields)
            session = message.get("session_name")
            if session:
                binding["session_name"] = session
            if request.get("reply_to") is None and isinstance(fields.get("exchange"), dict):
                # Snapshot each agent participant's last known session with the new exchange.
                for participant in (fields["exchange"]["requester"], fields["exchange"]["responder"]):
                    if participant.get("kind") != "agent":
                        continue
                    other = binding if participant["name"] == binding["name"] else allocation.find_agent(
                        state, participant["name"])
                    known = session if other is binding else (other or {}).get("session_name")
                    if known:
                        participant["session"] = known
                if fields["recipient"].get("kind") == "agent":
                    peer = allocation.find_agent(state, fields["recipient"]["name"])
                    if peer and peer.get("session_name"):
                        fields["recipient"]["session"] = peer["session_name"]
            private_dir(self.paths.state)
            try:
                with FileLock(self.paths.state / "history.lock"):
                    message_history.append(self.paths.state, message,
                                           max_entries=self.config.get("history_size", message_history.MAX_ENTRIES))
            except (OSError, ValueError):
                raise CommsError("Could not save message history; message was not accepted.") from None
            return message, warnings

        try:
            return store.update(operation)
        except allocation.AllocationError as exc:
            raise CommsError(f"{exc} Message was not queued.") from None

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
        return {"muted": self.audio_muted, "remaining_seconds": remaining,
                "volume": self.audio_volume,
                "volume_notice_until": self.volume_notice_until}

    def set_audio(self, action, seconds=None):
        if action not in ("status", "toggle", "mute", "unmute", "cycle-volume"):
            raise CommsError("Unknown audio action.")
        if seconds is not None and (action != "mute" or type(seconds) not in (int, float)
                                    or not math.isfinite(seconds) or not 0 < seconds <= 86400):
            raise CommsError("--seconds must be greater than 0 and at most 86400, for mute only.")
        if action == "status":
            return self.audio_status()
        if action == "cycle-volume":
            # Mute is a real shared speech state, not zero playback gain.
            # Reuse the normal transition below to cancel timers/release leases.
            levels = (10, 25, 50, 100)
            if not self.audio_muted and self.audio_volume == levels[-1]:
                action = "mute"
                self.volume_notice_until = 0
            else:
                self.audio_volume = (levels[0] if self.audio_muted else
                                     levels[levels.index(self.audio_volume) + 1])
                action = "unmute"
                self.volume_notice_until = time.monotonic() + 2
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

    def queue_preview(self):
        pending = list(self.pending.values())
        durations = [self.estimated_message_seconds(m) for m in pending]
        wait = max(0, self.estimated_total_seconds() - sum(durations))
        result = []
        for index, (message, duration) in enumerate(zip(pending, durations)):
            item = {"name": message["name"], "uid": message["uid"]}
            # Bound previews independently of queue length and message size.
            if index < 5:
                item.update(msg=message["msg"][:400] + ("…" if len(message["msg"]) > 400 else ""), recipient=message.get("recipient"),
                            wait_seconds=round(wait, 1), duration_seconds=round(duration, 1))
            result.append(item)
            wait += duration
        return result

    def snapshot(self):
        # The allocation document is atomically replaced. Read only after a change,
        # including external bag resets and newly allocated identities.
        try:
            stat = (self.paths.state / allocation.STATE_FILE).stat()
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except FileNotFoundError:
            stamp = None
        if stamp != self.presentation_stamp or not self.presentation:
            self.presentation = tree_view.presentation(allocation.read_state(self.paths.state))
            self.presentation_stamp = stamp
        return {"ok": True, "protocol_version": VERSION, "event": "state", "listener_generation": self.generation,
                "message": dict(self.current) if self.current else None,
                "audio": self.audio_status(), "lifecycle": dict(self.lifecycle),
                "presentation": self.presentation,
                "queued": self.queue_preview()}

    def publish(self, message, event, message_id=None):
        """Advance the shared lifecycle: start, display (progress timing), end."""
        self.current = message
        self.lifecycle = {"timeline": self.generation, "seq": self.lifecycle["seq"] + 1, "event": event,
                          "message_id": message_id if message_id is not None else (message or {}).get("uid")}
        self.display.show(message)
        self.broadcast()

    def broadcast(self, *, presentation_only=False):
        snapshot = None
        for writer, queue in list(self.watchers.items()):
            if presentation_only and writer not in self.presentation_watchers:
                continue
            try:
                if snapshot is None:
                    snapshot = self.snapshot()
                queue.put_nowait(snapshot)
            except asyncio.QueueFull:
                writer.close()  # A slow subscriber must not hold up global delivery.

    def estimated_message_seconds(self, message):
        if self.audio_muted:
            return max(self.config.get("display_seconds", 5), speech.estimate_seconds(message["msg"]))
        return 0.65 + speech.estimate_seconds(message["tts"])

    def estimated_total_seconds(self):
        now = time.monotonic()
        remaining = 0
        if self.current:
            if self.current["phase"] == "displaying":
                remaining = max(0, self.current["started_at"] + self.current["duration"]
                                + (0.15 if self.audio_duration else 0) - now)
            else:
                elapsed = max(0, now - self.preparing_started_at) if self.preparing_started_at is not None else 0
                if self.audio_muted:
                    remaining = self.estimated_message_seconds(self.current)
                else:
                    remaining = max(0, 0.65 - elapsed) + speech.estimate_seconds(self.current["tts"])
        return remaining + sum(self.estimated_message_seconds(message) for message in self.pending.values())

    async def process(self):
        while True:
            message = await self.queue.get()
            self.pending.pop(message["uid"], None)
            self.preparing_started_at = time.monotonic()
            self.claimed.clear()
            self.ready.clear()
            self.lease, self.audio_duration = None, None
            self.publish({**message, "phase": "preparing"}, "start")
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
            self.publish({**self.current, "phase": "displaying", "started_at": time.monotonic() + delay,
                          "duration": duration}, "display")
            await asyncio.sleep(duration + delay + (0.15 if self.audio_duration else 0))
            self.queue.task_done()
            if self.queue.empty():
                self.publish(None, "end", message["uid"])
            else:
                self.publish(self.current, "end")

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
                env = await asyncio.to_thread(request_owner_env, request)
                await asyncio.to_thread(checked_context, env)
                result = await asyncio.to_thread(pane_binding, self.paths, self.config, clear=True, env=env)
                result = {**result, "id": result.get("name")}  # Compatibility alias for name.
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "result": result})
            elif request.get("command") == "context":
                env = await asyncio.to_thread(request_owner_env, request)
                # File locks and tuios queries must not stall speech/watch clients.
                try:
                    identity = await asyncio.to_thread(identify, self.paths, self.config, env)
                except (CommsError, OSError) as exc:
                    raise CommsError(f"Listener identity lookup failed: {exc}") from None
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "identity": identity})
            elif request.get("command") == "shell-binding":
                if "shell_pid" not in request:
                    raise CommsError("Missing caller process ID.")
                env = await asyncio.to_thread(request_owner_env, request)
                try:
                    epoch, binding = await asyncio.to_thread(pane_binding, self.paths, self.config, allocate=False, env=env)
                    name = binding["name"]
                except ContextMismatch:
                    epoch, name = None, None
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "id": name, "name": name,
                                           "epoch": epoch})
            elif request.get("command") == "send":
                if "pane_env" not in request and "shell_pid" not in request:
                    raise CommsError("Missing caller ownership; upgrade the agent-comms client. Message was not queued.")
                if self.queue.qsize() + self.accepting >= self.queue.maxsize:
                    raise CommsError("Message queue is full; try again later.") from None
                self.accepting += 1
                try:
                    env = await asyncio.to_thread(request_owner_env, request)
                    # File locks and shell inspection must not stall speech/watch clients.
                    message, warnings = await asyncio.to_thread(self.accept, request, env)
                finally:
                    self.accepting -= 1
                self.pending[message["uid"]] = message
                self.queue.put_nowait(message)
                self.broadcast(presentation_only=True)
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "message_id": message["uid"],
                                           "warnings": warnings,
                                           "estimated_total_seconds": self.estimated_total_seconds()})
                self.schedule_title_refresh(message["name"], request.get("pane_env"))
            elif request.get("command") in ("role", "tree"):
                env = await asyncio.to_thread(request_owner_env, request)
                store = pane_identity.store(self.paths, lock=FileLock, atomic_json=atomic_json, private_dir=private_dir)
                def change(state):
                    key = pane_identity.owner(env)[0]
                    binding = (state or {}).get("bindings", {}).get(key)
                    if binding is None:
                        raise CommsError("Run agent-comms context first.")
                    try:
                        result = tree_view.change(state, binding, request)
                    except ValueError as exc:
                        raise CommsError(str(exc)) from None
                    return result, tree_view.presentation(state)
                result, self.presentation = await asyncio.to_thread(store.update, change)
                self.broadcast(presentation_only=True)
                await write_frame(writer, {"ok": True, "protocol_version": VERSION, "result": result})
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
                visualizer = request.get("visualizer")
                if duration is not None and audio_visualizer.decode(visualizer):
                    self.current["visualizer"] = visualizer
                self.audio_duration = duration
                cost = request.get("credit_cost")
                if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                    self.current["credit_cost"] = cost
                self.ready.set()
                await write_frame(writer, {"ok": True, "protocol_version": VERSION})
            elif request.get("command") == "watch":
                updates = asyncio.Queue(maxsize=16)
                self.watchers[writer] = updates
                if request.get("presentation") is True:
                    self.presentation_watchers.add(writer)
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
                await write_frame(writer, {"ok": False, "error": str(exc),
                                           **({"context_mismatch": True} if isinstance(exc, ContextMismatch) else {})})
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            self.watchers.pop(writer, None)
            self.presentation_watchers.discard(writer)
            await close_writer(writer)
            self.tasks.discard(task)

    async def run(self):
        private_dir(self.paths.runtime)
        discovery = self.paths.runtime / "server.json"
        with FileLock(self.paths.runtime / "server.lock", wait=False):
            listener = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=MAX_FRAME)
            processor = None
            try:
                atomic_json(discovery, {"protocol_version": VERSION, **CAPABILITIES,
                                        "port": listener.sockets[0].getsockname()[1], "token": self.token})
                with TerminalDisplay(self.quiet) as self.display, stop_signals(self.stop):
                    self.publish(None, "idle")
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
        target.add_argument("--config", default=default, help="Config TOML path (AGENT_COMMS_CONFIG)")
        target.add_argument("--state-dir", default=default, help="Allocation/history state directory (AGENT_COMMS_STATE_DIR)")
        target.add_argument("--runtime-dir", default=default, help="Discovery/lock directory (AGENT_COMMS_RUNTIME_DIR)")
    common(result)
    subs = result.add_subparsers(dest="command", required=True)
    identity = subs.add_parser("context", help="Return the caller's generated name, reset epoch, and required prefix as JSON")
    identity_actions = identity.add_mutually_exclusive_group()
    identity_actions.add_argument("--refill", action="store_true", help=argparse.SUPPRESS)  # Retired; reports guidance.
    identity_actions.add_argument("--clear", action="store_true", help="Release the current pane or shell's name; the next context gets a fresh name")
    identity_actions.add_argument("--clear-all", action="store_true", help="Release all pane and shell bindings; issued names stay spent")
    bags = subs.add_parser("bags", help="Inspect or explicitly reset the name and voice bags")
    bag_actions = bags.add_subparsers(dest="action", required=True)
    bag_status = bag_actions.add_parser("status", help="Show generation, remaining bag sizes, and bindings as JSON")
    bag_reset = bag_actions.add_parser("reset", help="Start a new epoch at generation 0; clears all bindings (also migrates legacy state)")
    bag_reset.add_argument("--confirm", action="store_true", help="Required: names may repeat and cached contexts become invalid")
    review = subs.add_parser("voice-review", help="Audition non-approved voice profiles and save approval decisions")
    review.add_argument("--all", action="store_true", help="Audition all active voice profiles, including approved voices")
    serve_parser = subs.add_parser("serve", help="Run the single per-user listener")
    serve_parser.add_argument("--quiet", action="store_true", help="Disable the standalone terminal display")
    service = subs.add_parser("service", help="Manage the per-user OS service (macOS launchd / Windows Task Scheduler)")
    restart = subs.add_parser("restart", help="Reload watchers, optionally restarting the listener service first")
    restart.add_argument("scope", choices=("all", "watchers"), nargs="?", default="all")
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
    sender = send_parser.add_mutually_exclusive_group()
    sender.add_argument("--name", help="Expected caller name returned by context")
    sender.add_argument("--id", help="Deprecated alias for --name (accepts the generated name)")
    send_parser.add_argument("--epoch", help="Reset epoch returned by context")
    send_parser.add_argument("--msg", required=True)
    send_parser.add_argument("--tts", help="Speech text including optional ElevenLabs tags; defaults to --msg")
    send_parser.add_argument("--display-recipient", metavar="NAME",
                             help="Displayed recipient only; does not route or deliver the message. "
                                  "Defaults to the configured user.")
    send_parser.add_argument("--role", choices=conversation.ROLES, help="Your side of the displayed exchange (default: requester)")
    send_parser.add_argument("--kind", choices=conversation.KINDS, help="Displayed message kind (default: statement)")
    send_parser.add_argument("--emotion", help=f"Displayed expression: {', '.join(conversation.FACES)} (default: neutral)")
    send_parser.add_argument("--reply-to", metavar="MESSAGE_ID", help="Continue the displayed exchange of an accepted message")
    watcher = subs.add_parser("watch", help="Stream captions or show a silent conversation/tree/log view")
    watcher.add_argument("--view", choices=("captions", "conversation", "tree", "log"), default="captions",
                         help="captions: dock caption line with speech (default); conversation/tree/log: silent pane-sized views")
    watcher.add_argument("--tree", default="default", help="Named tree to display (default: default)")
    watcher.add_argument("--no-animation", action="store_true", help="Disable arrival slides in conversation and log views")
    watcher.add_argument("--no-color", action="store_true")
    watcher.add_argument("--width", type=int, default=96, help="Maximum display cells (default: 96)")
    watcher.add_argument("--mute", action="store_true", help="Display captions without generating or playing speech")
    effects = watcher.add_mutually_exclusive_group()
    effects.add_argument("--vfx", dest="vfx", action="store_true", help="Show live audio across the caption background (default)")
    effects.add_argument("--no-vfx", dest="vfx", action="store_false", help="Disable delivery animation; use --no-flash too for no motion")
    watcher.set_defaults(vfx=True)
    attention = watcher.add_mutually_exclusive_group()
    attention.add_argument("--flash", dest="flash", action="store_true", help="Flash briefly for new messages (default)")
    attention.add_argument("--no-flash", dest="flash", action="store_false", help="Disable the brief attention flash for new messages")
    watcher.set_defaults(flash=True)
    audio = subs.add_parser("audio", help="Control voice live across all watchers; captions stay enabled")
    audio.add_argument("action", choices=("status", "toggle", "mute", "unmute", "cycle-volume", "dock-click"))
    audio.add_argument("--seconds", type=float, help="Automatically unmute after this many seconds (mute only)")
    history = subs.add_parser("history", help="Print previous accepted messages, even when the listener is offline")
    history.add_argument("--limit", type=int, help="Number of messages to show (defaults to history_size)")
    history_name = history.add_mutually_exclusive_group()
    history_name.add_argument("--name", help="Filter by generated agent name")
    history_name.add_argument("--id", help="Deprecated alias: filter by record ID (the name, or a legacy identity ID)")
    history.add_argument("--epoch", help="Filter by reset epoch, disambiguating repeated names")
    history.add_argument("--session", help="Filter by tuios session name")
    history_format = history.add_mutually_exclusive_group()
    history_format.add_argument("--json", action="store_true", help="Print machine-readable full records including speech text")
    history_format.add_argument("--raw", action="store_true", help="Print unstyled timestamp/name/message lines")
    history.add_argument("--no-color", action="store_true", help="Disable history heading colors")
    instructions = subs.add_parser("instructions", help="Print shared communication instructions without allocating identity")
    instructions.add_argument("--format", choices=("json", "text"), default="json")
    role = subs.add_parser("role", help="Set your persistent display role (independent of send --role)")
    role.add_argument("value", nargs="?")
    role.add_argument("--clear", action="store_true")
    tree = subs.add_parser("tree", help="Manage a named display tree owned by your identity")
    tree.add_argument("action", choices=("set", "show", "delete"))
    tree.add_argument("tree_name")
    tree.add_argument("--title", help="Main screen title (set only); empty string restores the tree name")
    tree.add_argument("--file", help="JSON array of nodes, or '-' for stdin (set only)")
    for sub in (identity, bags, bag_status, bag_reset, review, serve_parser, service, restart, send_parser, watcher,
                instructions, history, audio, role, tree):
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


def control_pressed():
    """Tuios click hooks omit modifiers; sample the local keyboard state."""
    import ctypes
    if sys.platform == "darwin":
        quartz = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        flags = quartz.CGEventSourceFlagsState
        flags.argtypes = [ctypes.c_int32]
        flags.restype = ctypes.c_uint64
        return bool(flags(0) & (1 << 18))  # Combined session state, Control flag.
    if os.name == "nt":
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x11) & 0x8000)
    return False


def main(argv=None):
    prepare_output()
    args = parser().parse_args(argv)
    try:
        paths = Paths.resolve(args)
        restart_all = args.command == "restart" and args.scope == "all"
        if restart_all:
            args.command, args.action = "service", "restart"
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
            if restart_all:
                restart_watchers(paths)
                print("Watcher reload requested.")
        elif args.command == "restart":
            restart_watchers(paths)
            print("Watcher reload requested; listener unchanged.")
        elif args.command == "context" and args.refill:
            raise CommsError("context --refill was retired: names refill automatically when the bag is exhausted. "
                             "For an explicit new epoch at generation 0, use 'agent-comms bags reset --confirm'.")
        elif args.command == "bags":
            config = load_config(paths.config)
            store = pane_identity.store(paths, lock=FileLock, atomic_json=atomic_json, private_dir=private_dir)
            try:
                if args.action == "status":
                    try:
                        state = store.read()
                    except allocation.LegacyState as exc:
                        print(json.dumps({"initialized": False, "legacy": True, "guidance": str(exc)}))
                        return 0
                    print(json.dumps(allocation.status(state, config), ensure_ascii=True))
                else:
                    if not args.confirm:
                        raise CommsError("bags reset needs --confirm: it clears every binding, returns names to "
                                         "generation 0 (names may repeat), and invalidates cached contexts.")
                    result = store.reset(config)
                    print(json.dumps(result, ensure_ascii=True))
                    print("agent-comms: Reset complete. Cached contexts are invalid; agents must run "
                          "'agent-comms context' again. Names may now repeat earlier names.", file=sys.stderr)
            except allocation.AllocationError as exc:
                raise CommsError(str(exc)) from None
        elif args.command == "context" and args.clear_all:
            result = pane_identity.clear_all(paths, private_dir=private_dir, lock=FileLock,
                                             atomic_json=atomic_json, error=CommsError)
            print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        elif args.command == "audio":
            if args.action == "dock-click":
                button = os.environ.get("TUIOS_CLICK_BUTTON", "1")
                if button != "1":
                    return 0
                args.action = "cycle-volume" if control_pressed() else "mute"
                args.seconds = 15 if args.action == "mute" else None
            if args.seconds is not None and (args.action != "mute" or not math.isfinite(args.seconds) or not 0 < args.seconds <= 86400):
                raise CommsError("--seconds must be greater than 0 and at most 86400, for mute only.")
            if endpoint(paths).get("audio_control") is not True:
                raise CommsError("Listener predates live audio controls; restart 'agent-comms serve' and existing watchers once.")
            response = asyncio.run(rpc(paths, "audio", action=args.action, seconds=args.seconds))
            print(json.dumps(response["audio"]))
        elif args.command == "history":
            config = load_config(paths.config) if paths.config.exists() else {}
            history_size = config.get("history_size", message_history.MAX_ENTRIES)
            limit = history_size if args.limit is None else args.limit
            if not 1 <= limit <= history_size:
                raise CommsError(f"--limit must be between 1 and {history_size}.")
            if args.id is not None:
                print("agent-comms: history --id is deprecated; use --name.", file=sys.stderr)
            filters = {"identity": args.id, "name": args.name, "epoch": args.epoch, "session": args.session}
            messages = message_history.read(paths.state, limit, max_entries=history_size, **filters)
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
            if args.view in ("conversation", "tree", "log") and not sys.stdout.isatty():
                raise CommsError("This view is pane-sized and requires a terminal.")
            try:
                settings = load_config(paths.config).get("speech", {})
            except CommsError:
                if paths.config.exists():
                    raise  # Never turn an invalid local-mode config into cloud speech.
                settings = {}
            reload_watcher = asyncio.run(watch(
                paths, color=not args.no_color and "NO_COLOR" not in os.environ, width=args.width,
                muted=args.mute, flash=args.flash, settings=settings, vfx=args.vfx,
                view=args.view, animate=not args.no_animation, tree=args.tree))
            if reload_watcher:
                original_args = list(sys.argv[1:] if argv is None else argv)
                os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *original_args])
        else:
            config = load_config(paths.config)
            if args.command == "context":
                if args.clear:
                    print(json.dumps(asyncio.run(listener_context(paths, clear=True)), ensure_ascii=True, separators=(",", ":")))
                else:
                    print(json.dumps(asyncio.run(listener_context(paths)), ensure_ascii=True, separators=(",", ":")))
            elif args.command in ("role", "tree"):
                env = {k: os.environ[k] for k in PANE_ENV_KEYS if k in os.environ}
                owner = {"pane_env": env} if env.get("TUIOS_ENV") == "1" else {"shell_pid": os.getpid()}
                if args.command == "role":
                    if args.clear == (args.value is not None):
                        raise CommsError("Supply a role string or --clear.")
                    payload = {"value": "" if args.clear else args.value}
                else:
                    payload = {"action": args.action, "tree_name": args.tree_name}
                    if args.action == "set":
                        if not args.file and args.title is None:
                            raise CommsError("tree set requires --file JSON_FILE and/or --title TEXT.")
                        if args.title is not None:
                            payload["title"] = args.title
                        if args.file:
                            try:
                                payload["nodes"] = json.loads(sys.stdin.read() if args.file == "-" else Path(args.file).read_text())
                            except (OSError, ValueError) as exc:
                                raise CommsError(f"Cannot read tree: {exc}") from None
                    elif args.file or args.title is not None:
                        raise CommsError("--file and --title are only valid with tree set.")
                require_capability(endpoint(paths), "tree_view", "Restart agent-comms listener to enable trees and roles.")
                print(json.dumps(asyncio.run(rpc(paths, args.command, **payload, **owner))["result"]))
            elif args.command == "voice-review":
                voice_review(paths, config, all_voices=args.all)
            elif args.command == "send":
                if args.name is None and args.id is None:
                    raise ContextMismatch("send needs --name NAME --epoch EPOCH from 'agent-comms context'. "
                                          "Message was not queued.")
                if args.id is not None:
                    print("agent-comms: --id is deprecated; use --name with the generated name.", file=sys.stderr)
                expected = args.name if args.name is not None else args.id
                display = {"display_recipient": args.display_recipient, "role": args.role, "kind": args.kind,
                           "emotion": args.emotion, "reply_to": args.reply_to}
                try:
                    conversation.validate_request(display)
                except conversation.ConversationError as exc:
                    raise CommsError(f"{exc} Message was not queued.") from None
                source = checked_context()
                epoch, name = caller_binding(paths)
                if expected != name:
                    raise ContextMismatch("Name mismatch: --name does not match the caller pane or shell's "
                                          "current name. Message was not queued.")
                if args.epoch is None:
                    raise ContextMismatch("Missing --epoch: send needs the epoch returned by context. "
                                          "Message was not queued.")
                if args.epoch != epoch:
                    raise ContextMismatch("Epoch mismatch: this context predates an explicit bag reset. "
                                          "Message was not queued.")
                validate_message(config, name, args.msg, args.tts, source)
                result = asyncio.run(send(paths, name, epoch, args.msg, args.tts, source, display))
                for warning in result.get("warnings", []) if isinstance(result, dict) else []:
                    print(f"agent-comms: warning: {clean_text(str(warning))}", file=sys.stderr)
                seconds = result.get("estimated_total_seconds") if isinstance(result, dict) else None
                if seconds is None:
                    print("Message queued. Total time unavailable; restart the listener to enable estimates.")
                else:
                    print(f"Estimated total time: {seconds:.1f} seconds (approximate).")
                if isinstance(result, dict) and isinstance(result.get("message_id"), str):
                    print(f"Message ID: {result['message_id']}")
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
            print("agent-comms: Use this up-to-date name, epoch, and prefix; recompose both --msg and --tts "
                  "and retry once with --name NAME --epoch EPOCH. No separate context lookup is needed. "
                  "Use a nonempty prefix exactly once at the start of both arguments.", file=sys.stderr)
        return 1
    except (CommsError, service_manager.ServiceError, OSError, asyncio.TimeoutError) as exc:
        print(f"agent-comms: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
