"""Private JSON-lines worker, run by Pocket's isolated Python (not the listener)."""
import argparse
from contextlib import redirect_stdout
import json
import sys


def load_model(language):
    from pocket_tts import TTSModel
    return TTSModel.load_model(language=language)


def write_audio(path, chunks, sample_rate):
    from pocket_tts.data.audio import stream_audio_chunks

    def bounded():
        samples = 0
        for chunk in chunks:
            samples += chunk.numel()
            if samples > sample_rate * 600:
                raise ValueError("Audio exceeds duration limit")
            yield chunk

    stream_audio_chunks(path, bounded(), sample_rate)


class PocketSynthesizer:
    def __init__(self, language, *, loader=load_model, writer=write_audio):
        self.language, self.loader, self.writer = language, loader, writer
        self.model = None
        self.voices = {}  # Empty until a request actually uses a voice.

    def synthesize(self, voice, text, path):
        if self.model is None:
            self.model = self.loader(self.language)
        hit = voice in self.voices
        if not hit:
            self.voices[voice] = self.model.get_state_for_audio_prompt(voice)
        chunks = self.model.generate_audio_stream(
            model_state=self.voices[voice], text_to_generate=text, copy_state=True)
        self.writer(path, chunks, self.model.config.mimi.sample_rate)
        return {"ok": True, "voice_cache_hit": hit, "cached_voices": len(self.voices)}


def serve(source, destination, language):
    synth = PocketSynthesizer(language)
    while line := source.readline(65537):
        if len(line) > 65536:
            return 1
        try:
            request = json.loads(line)
            if (not isinstance(request, dict) or any(
                    not isinstance(request.get(k), str) or not request[k].strip()
                    for k in ("voice", "text", "path"))):
                raise ValueError("Invalid synthesis request")
            # Libraries must never mix logs or generated text into the protocol.
            with redirect_stdout(sys.stderr):
                result = synth.synthesize(request["voice"], request["text"], request["path"])
        except Exception:
            # Do not return private text, paths, or library diagnostics.
            result = {"ok": False}
        destination.write(json.dumps(result) + "\n")
        destination.flush()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default="english")
    args = parser.parse_args()
    sys.exit(serve(sys.stdin, sys.stdout, args.language))
