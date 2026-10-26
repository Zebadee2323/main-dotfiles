"""Naia-style estimated captions and optional ElevenLabs PCM speech playback."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import json
import wave

SAMPLE_RATE = 24000
DEFAULT_MODEL = "eleven_turbo_v2_5"  # Same default as Naia features/voice/runtime.lua.
MAX_SECONDS = 600
MAX_AUDIO_BYTES = SAMPLE_RATE * 2 * MAX_SECONDS
REQUEST_TIMEOUT = 25


class SpeechError(Exception):
    pass


def characters(text):
    """Keep combining marks with their base, as Naia's strcharpart(..., 1) does."""
    result = []
    for char in text:
        if unicodedata.category(char) in ("Mn", "Me") and result:
            result[-1] += char
        else:
            result.append(char)
    return result


def width(text):
    return sum(0 if unicodedata.category(c) in ("Mn", "Me") else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def estimate_seconds(text):
    # Port of nvim/lua/features/voice/captions.lua: ~180 words/minute.
    return sum(len(characters(word)) for word in text.split()) / 15


def pages(text, columns):
    """Naia word wrapping and character-weighted pages, one dock row per page."""
    columns = max(1, columns)
    lines, line = [], ""

    def flush():
        nonlocal line
        if line:
            lines.append(line)
            line = ""

    for word in text.split():
        candidate = line + " " + word if line else word
        if width(candidate) <= columns:
            line = candidate
        else:
            flush()
            for char in characters(word):
                if width(line + char) > columns:
                    flush()
                line += char
    flush()
    total, result = 0, []
    for line in lines:
        total += len(characters(line))
        result.append((line, total))
    return result or [("", 1)]


def page_at(prepared, elapsed, duration):
    target = max(0, min(1, elapsed / max(duration, 0.01))) * prepared[-1][1]
    return next((line for line, finish in prepared if target < finish), prepared[-1][0])


@dataclass
class Audio:
    path: Path
    duration: float

    def remove(self):
        self.path.unlink(missing_ok=True)


def synthesize(directory, voice_id, text, api_key, model=DEFAULT_MODEL, speed=1.0):
    voice = urllib.parse.quote(voice_id, safe="")
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_{SAMPLE_RATE}",
        data=json.dumps({"text": text, "model_id": model, "voice_settings": {"speed": speed}}).encode(),
        headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            pcm = response.read(MAX_AUDIO_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # Do not log request headers, API keys, response bodies, or spoken text.
        raise SpeechError(f"ElevenLabs returned HTTP {exc.code}.") from None
    except (OSError, urllib.error.URLError, ValueError):
        raise SpeechError("ElevenLabs request failed.") from None
    if not pcm or len(pcm) > MAX_AUDIO_BYTES or len(pcm) % 2:
        raise SpeechError("ElevenLabs returned invalid or oversized PCM audio.")
    fd, filename = tempfile.mkstemp(prefix="speech-", suffix=".wav", dir=directory)
    os.close(fd)
    path = Path(filename)
    try:
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(SAMPLE_RATE)
            stream.writeframes(pcm)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return Audio(path, len(pcm) / (SAMPLE_RATE * 2))


def player():
    if os.name == "nt":
        return "winsound"
    if sys.platform == "darwin" and shutil.which("afplay"):
        return "afplay"
    if shutil.which("ffplay"):
        return "ffplay"
    if shutil.which("aplay"):
        return "aplay"
    return None


async def play(audio, executable):
    if executable == "winsound":
        import winsound
        winsound.PlaySound(str(audio.path), winsound.SND_FILENAME | winsound.SND_ASYNC)
        try:
            await asyncio.sleep(audio.duration)
        finally:
            winsound.PlaySound(None, 0)
        return
    command = [executable]
    if executable == "ffplay":
        command += ["-nodisp", "-autoexit", "-loglevel", "error"]
    elif executable == "aplay":
        command += ["-q"]
    process = await asyncio.create_subprocess_exec(*command, str(audio.path),
                                                 stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
    try:
        result = await asyncio.wait_for(process.wait(), audio.duration + 5)
        if result:
            raise SpeechError("Audio player failed.")
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 1)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
