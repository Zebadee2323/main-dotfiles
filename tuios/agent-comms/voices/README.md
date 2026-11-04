# Local Pocket TTS voices

Store reference recordings (`.wav`) or exported conditioning states
(`.safetensors`) here. Only use recordings you have permission to clone, and
check permission before committing or sharing voice files.

In an identity's `config.toml` section:

```toml
pocket_tts_voice = "voices/my-voice.safetensors"
pocket_tts_voice_approved = false
```

Paths are relative to `config.toml`, independent of the listener's working
directory. Run `agent-comms voice-review` to audition and approve the voice.

From the `agent-comms` directory:

```sh
pocket-tts export-voice --language english /path/to/reference.wav voices/my-voice.safetensors
```

Export uses the first 30 seconds at most. Export with the same model/language
used for synthesis; re-export when changing models. A reference WAV can also be
used directly without exporting. Custom cloning may require accepting upstream
model terms and Hugging Face authentication.
