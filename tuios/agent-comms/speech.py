"""Naia-style captions with ElevenLabs, Phonon, or Pocket TTS speech playback."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import os
import re
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
PHONON_MODEL = "hf://gradium/phonon-31m-en"
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


async def synthesize_phonon(directory, voice, text, settings=None, speed=1.0):
    """Run the optional ptts CLI without a shell; cancellation stops the writer."""
    settings = settings or {}
    executable = shutil.which(os.path.expanduser(settings.get("executable", "ptts")))
    if not executable:
        raise SpeechError("Phonon requires ptts; install it with 'uv tool install ptts'.")
    text = plain_speech(text)
    return await synthesize_local(
        directory, "Phonon", lambda path: [
            executable, "--model", settings.get("model", PHONON_MODEL),
            "--lang", "en", "--quant", "q8_0", "--device", "cpu",
            "--voice", voice, "--output", str(path), "--", text], speed)


def plain_speech(text):
    # Existing agents may send ElevenLabs SSML. Do not read that markup aloud.
    return re.sub(r"<break\b[^>]*?/?>", " ", text, flags=re.IGNORECASE)


async def synthesize_pocket_tts(directory, voice, text, settings=None, speed=1.0):
    """Optional Pocket TTS CLI; built-in names or consented local reference audio."""
    settings = settings or {}
    executable = shutil.which(os.path.expanduser(settings.get("executable", "pocket-tts")))
    if not executable:
        raise SpeechError("Pocket TTS requires pocket-tts; install with 'uv tool install --python 3.11 pocket-tts'.")
    voice = os.path.expanduser(voice)
    return await synthesize_local(
        directory, "Pocket TTS", lambda path: [
            executable, "generate", "--language", settings.get("language", "english"),
            "--device", "cpu", "--voice", voice, "--output-path", str(path),
            "--quiet", "--text", "-"], speed, input_data=plain_speech(text).encode())


class PocketWorker:
    """Watcher-owned model process with lazy voice caching and serialized requests."""
    def __init__(self, settings=None):
        self.settings = settings or {}
        self.process = None
        self.lock = asyncio.Lock()
        self.last_stats = None

    def interpreter(self):
        configured = self.settings.get("python_executable")
        if configured:
            interpreter = shutil.which(os.path.expanduser(configured))
        else:
            executable = shutil.which(os.path.expanduser(self.settings.get("executable", "pocket-tts")))
            if not executable:
                raise SpeechError("Pocket TTS requires pocket-tts; install with 'uv tool install --python 3.11 pocket-tts'.")
            # uv's console script resolves into its isolated environment's bin/Scripts.
            name = "python.exe" if os.name == "nt" else "python"
            interpreter = shutil.which(str(Path(executable).resolve().with_name(name)))
        if not interpreter:
            raise SpeechError("Cannot find Pocket TTS Python; set speech.pocket_tts.python_executable.")
        return interpreter

    async def _request(self, voice, text, path):
        if self.process is None or self.process.returncode is not None:
            self.process = await asyncio.create_subprocess_exec(
                self.interpreter(), "-u", str(Path(__file__).with_name("pocket_worker.py")),
                "--language", self.settings.get("language", "english"),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=4096)
        self.process.stdin.write((json.dumps({"voice": voice, "text": plain_speech(text),
                                               "path": str(path)}) + "\n").encode())
        await self.process.stdin.drain()
        try:
            response = await self.process.stdout.readline()
            result = json.loads(response)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("Worker synthesis failed")
        except (ValueError, UnicodeError):
            raise SpeechError("Pocket TTS worker failed; check the model, voice, and cached downloads.") from None
        self.last_stats = {key: result.get(key) for key in ("voice_cache_hit", "cached_voices")}

    async def _close(self):
        process, self.process = self.process, None
        self.last_stats = None
        if process is not None:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
            await process.wait()

    async def close(self):
        async with self.lock:
            await self._close()

    async def synthesize(self, directory, voice, text, speed=1.0):
        fd, filename = tempfile.mkstemp(prefix="speech-pocket-", suffix=".wav", dir=directory)
        os.close(fd)
        path = Path(filename)
        try:
            async with self.lock:
                try:
                    await asyncio.wait_for(self._request(os.path.expanduser(voice), text, path), REQUEST_TIMEOUT)
                except BaseException:
                    # Kill the writer before unlinking; no late replies or corrupt cached state.
                    # A future request starts a new model with an empty voice cache.
                    await self._close()
                    raise
            audio = read_local_audio(path, "Pocket TTS")
            if speed != 1.0:
                processed = await filter_audio(audio, f"atempo={speed}", "Pocket TTS voice speed")
                audio.remove()
                return processed
            return audio
        except BaseException:
            path.unlink(missing_ok=True)
            raise


def read_local_audio(path, label):
    if path.stat().st_size > MAX_AUDIO_BYTES + 4096:
        raise SpeechError(f"{label} returned oversized audio.")
    try:
        with wave.open(str(path), "rb") as stream:
            frames = stream.getnframes()
            if (stream.getnchannels() != 1 or stream.getsampwidth() != 2
                    or stream.getframerate() != SAMPLE_RATE or frames <= 0
                    or frames > SAMPLE_RATE * MAX_SECONDS
                    or len(stream.readframes(frames)) != frames * 2):
                raise SpeechError(f"{label} returned invalid audio.")
            return Audio(path, frames / SAMPLE_RATE)
    except (wave.Error, EOFError):
        raise SpeechError(f"{label} returned invalid WAV audio.") from None


async def synthesize_local(directory, label, command, speed, *, input_data=None):
    """Bounded, cancellable local CLI generation; shared measured PCM validation."""
    fd, filename = tempfile.mkstemp(prefix="speech-local-", suffix=".wav", dir=directory)
    os.close(fd)
    path, process, audio = Path(filename), None, None
    try:
        process = await asyncio.create_subprocess_exec(
            *command(path),
            **({"stdin": asyncio.subprocess.PIPE} if input_data is not None else {}),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if input_data is None:
            code = await asyncio.wait_for(process.wait(), REQUEST_TIMEOUT)
        else:
            await asyncio.wait_for(process.communicate(input=input_data), REQUEST_TIMEOUT)
            code = process.returncode
        if code:
            raise SpeechError(f"{label} synthesis failed; check the model, voice, and cached downloads.")
        audio = read_local_audio(path, label)
        if speed != 1.0:
            # Preserve the shared speed multiplier exactly, without changing pitch.
            processed = await filter_audio(audio, f"atempo={speed}", f"{label} voice speed")
            audio.remove()
            return processed
        return audio
    except BaseException:
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        path.unlink(missing_ok=True)
        raise


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
