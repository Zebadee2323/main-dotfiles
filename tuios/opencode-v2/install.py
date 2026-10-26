#!/usr/bin/env python3
"""Preview by default; --apply installs without restarting any running process."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    cli = config / "opencode/cli.json"
    harness = Path(os.environ.get("TUIOS_HARNESS_DIR", config / "tuios/harnesses")) / "opencode.toml"
    settings = json.loads(cli.read_text()) if cli.exists() else {"$schema": "https://opencode.ai/v2/cli.json"}
    plugins = settings.setdefault("plugins", [])
    if not isinstance(plugins, list):
        raise SystemExit("cli.json plugins must be an array; no files changed")
    # Preserve other directives and plugin options. Explicitly disable the
    # managed integration rather than edit/delete files TUIOS owns.
    entry = str(source)
    plugins[:] = [p for p in plugins if p not in ("-tuios-agent-state", "-tuios-agent-state-v1")]
    if not any(p == entry or isinstance(p, dict) and p.get("package") == entry for p in plugins):
        plugins.append(entry)
    plugins.extend(["-tuios-agent-state", "-tuios-agent-state-v1"])
    manifest = (source / "opencode.toml").read_bytes()
    # Recognize only our exact original snapshot manifest for upgrade. An
    # independently edited custom manifest still requires a manual merge.
    original = manifest.replace(b'reader = "gemini-chat"', b'reader = "amp-thread"').replace(b'glob = "*.jsonl"', b'glob = "*.json"')
    if harness.exists() and harness.read_bytes() not in (manifest, original):
        raise SystemExit(f"Existing custom manifest differs: {harness}; merge it manually. No files changed.")
    print(f"Update {cli} (keep unrelated settings; disable the old TUIOS CLI plugin)")
    print(f"Install {harness} (local conversation reader)")
    print("No service, daemon, pane or permission settings will be changed.")
    if not args.apply:
        print("Preview only. Run again with --apply to install.")
        return
    cli.parent.mkdir(parents=True, exist_ok=True)
    harness.parent.mkdir(parents=True, exist_ok=True)
    destination = cli.resolve() if cli.is_symlink() else cli
    backup = destination.with_name(destination.name + ".before-tuios-v2")
    if destination.exists() and not backup.exists():
        shutil.copy2(destination, backup)
    # Atomic replacement preserves a dotfiles-managed cli.json symlink.
    fd, tmp = tempfile.mkstemp(dir=destination.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    if not harness.exists() or harness.read_bytes() != manifest:
        destination = harness.resolve() if harness.is_symlink() else harness
        fd, tmp = tempfile.mkstemp(dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(manifest)
            os.replace(tmp, destination)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    print("Installed. OpenCode reloads cli.json automatically (or use /reload).")
    print("TUIOS reads harness manifests at daemon startup. Restart it yourself when safe;")
    print("this installer deliberately does not interrupt your current panes.")


if __name__ == "__main__":
    main()
