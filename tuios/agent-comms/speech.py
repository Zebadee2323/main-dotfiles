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
# Consistent perceived speech volume across voices, with headroom for peaks.
NORMALIZE_FILTER = "loudnorm=I=-16:TP=-1.0:LRA=11"
# Exact graph/settings from nvim/lua/features/voice/runtime.lua (robot_voice_filter).
ROBOT_FILTER = ",".join((
    "asplit=4[dry][d1][d2][d3]",
    "[d1]adelay=22|22,volume=0.78[body]",
    "[d2]chorus=0.78:0.98:50|68|82:0.38|0.32|0.26:0.38|0.44|0.50:2.5|2.8|3.3,"
    "tremolo=6:0.05,adelay=12|12,volume=0.68[ai]",
    "[d3]chorus=0.65:0.9:32|48:0.30|0.24:0.30|0.36:1.8|2.1,volume=0.62[wide]",
    "[dry][body][ai][wide]amix=4:weights=1 0.82 0.66 0.6[mix]",
    "[mix]highpass=f=80",
    "equalizer=f=140:t=q:w=1.2:g=3.2",
    "equalizer=f=3200:t=q:w=1:g=2.8",
    "equalizer=f=6000:t=q:w=1:g=1.0",
    "acompressor=threshold=-20dB:ratio=2.4:attack=12:release=110",
    "equalizer=f=4500:t=q:w=2:g=-1.0",
    "volume=5dB",
    "alimiter=limit=0.92",
))


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


async def normalize(audio):
    """Prepare normalized PCM before caption timing starts; caller owns both files."""
    return await filter_audio(audio, NORMALIZE_FILTER, "Volume normalization")


async def robotize(audio):
    """Apply Naia's robot effects; caller owns input and returned output files."""
    return await filter_audio(audio, ROBOT_FILTER, "Robot voice filter")


async def filter_audio(audio, graph, label):
    """Run cancellable FFmpeg preprocessing and measure the resulting WAV."""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise SpeechError(f"{label} requires ffmpeg; keeping input audio.")
    fd, filename = tempfile.mkstemp(prefix="speech-processed-", suffix=".wav", dir=audio.path.parent)
    os.close(fd)
    path, process = Path(filename), None
    try:
        process = await asyncio.create_subprocess_exec(
            executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(audio.path), "-af", graph,
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await asyncio.wait_for(process.wait(), 30):
            raise SpeechError(f"{label} failed; keeping input audio.")
        try:
            with wave.open(str(path), "rb") as stream:
                duration = stream.getnframes() / stream.getframerate()
        except (wave.Error, EOFError):
            raise SpeechError(f"{label} produced invalid audio; keeping input audio.") from None
        if duration <= 0:
            raise SpeechError(f"{label} produced empty audio; keeping input audio.")
        return Audio(path, duration)
    except BaseException:
        # Stop the writer before removing its output, including on disconnect.
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        path.unlink(missing_ok=True)
        raise


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
