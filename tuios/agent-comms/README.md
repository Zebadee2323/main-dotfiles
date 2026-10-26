# agent-comms

Shared agent identities and a per-user FIFO message listener, with live colored
tuios dock output. Python **3.11+**, standard library only. See [SPEC.md](SPEC.md)
for the design.

## Run

macOS/Linux, from this directory:

```sh
chmod +x agent-comms
./agent-comms serve
```

In another terminal:

```sh
ID=$(./agent-comms id)
./agent-comms send --id "$ID" --msg "Hello from the agent dock!"
./agent-comms watch
```

For a command available everywhere, put this directory on PATH or symlink the
launcher (keep the source directory in place):

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/agent-comms" ~/.local/bin/agent-comms
```

On Windows, use `agent-comms.cmd` (requires the Python `py` launcher), or run
`python agent_comms.py ...` directly. Add this directory to PATH to use
`agent-comms` from other directories. Windows support is implemented but has not
been execution-tested yet. Modern Windows Terminal is recommended for ANSI
colors.

```powershell
$id = .\agent-comms.cmd id
.\agent-comms.cmd send --id $id --msg "Hello from Windows!"
```

## Commands

| Command | Behavior |
| --- | --- |
| `id` | Print only the next ID; refill and shuffle automatically when empty. |
| `id --refill` | Replace remaining entries with a new shuffled bag; no draw/output. |
| `serve` | Own the singleton listener, timed FIFO queue, and terminal display. |
| `serve --quiet` | Same listener without terminal display. |
| `send --id ID --msg TEXT` | Return success after queue acceptance, not display completion. |
| `watch` | Stay connected and print flushed, ANSI-colored replacement lines. |
| `watch --no-color --width 80` | Plain-text output, at most 80 display cells. |

`NO_COLOR` also disables watcher colors. Watch width defaults to 96 cells.
Errors go to stderr and return a nonzero exit status. Offline sends do not launch
a server. Offline watchers show `[! ] comms: offline` and reconnect with backoff
from 250 ms to 5 seconds. Ctrl+C stops the foreground command. The server also
handles SIGTERM for clean shutdown where the OS delivers it.

## Configuration and locations

Edit [config.toml](config.toml) in this directory. It is the default on all
platforms, resolved relative to the program rather than your working directory
(including when launched through a symlink). Restart the server after changing it.
An explicitly selected missing config is an error; it is not silently generated.
`watch` does not need a local copy of the identity configuration.

```toml
display_seconds = 5

[identities.naia]
name = "Naia"
eleven_labs_voice_id = ""
personality = "Friendly, clear, and practical."
```

The starter roster is Naia, Nova, Atlas, and Echo. Voice IDs are intentionally
empty; voice/personality are metadata only. IDs are not exclusive reservations.

Default locations:

| Platform | Config | Bag state | Discovery and server lock |
| --- | --- | --- | --- |
| macOS | `config.toml` beside the program | `~/Library/Application Support/agent-comms/state/` | `~/Library/Application Support/agent-comms/state/agent-comms-run/` |
| Linux | `config.toml` beside the program | `~/.local/state/agent-comms/` | `$XDG_RUNTIME_DIR/agent-comms-run/`, otherwise `~/.local/state/agent-comms/agent-comms-run/` |
| Windows | `config.toml` beside the program | `%LOCALAPPDATA%\agent-comms\state\` | `%LOCALAPPDATA%\agent-comms\run\` |

macOS/Linux honor `XDG_STATE_HOME` and `XDG_RUNTIME_DIR` when set. Windows falls
back to the user's `AppData/Local` if `LOCALAPPDATA` is absent. Config location is
independent of XDG settings; use the explicit config override below if needed.

Overrides, accepted before or after the subcommand:

| Flag | Environment variable |
| --- | --- |
| `--config PATH` | `AGENT_COMMS_CONFIG` |
| `--state-dir DIR` | `AGENT_COMMS_STATE_DIR` |
| `--runtime-dir DIR` | `AGENT_COMMS_RUNTIME_DIR` |

Use **dedicated user-private directories** for state/runtime overrides. On POSIX,
the program enforces mode 0700 on state/runtime directories; data files are
created with mode 0600. It does not modify config directory permissions or create
config lock files. On Windows, files inherit the containing user's profile ACLs;
do not redirect them into a shared directory. The token protects against other
local users, not processes already running as you.

The locked `bag.json` stores the roster and remaining IDs. Changing the set of
configured IDs starts a new cycle; changing metadata alone does not. Corrupt or
invalid bag contents are replaced with a fresh shuffled cycle. Invalid JSON
also produces a warning on stderr. The separate lock file is never replaced or
deleted while updating bag contents atomically.

## Transport and lifecycle

One server per selected per-user runtime directory binds only `127.0.0.1` on an
ephemeral port. `server.json` atomically advertises protocol version, port, and a
random 256-bit token. **Treat this file as a secret.** Clients reread it when
connecting. There is no manual port assignment, remote listener, or native
Windows/WSL discovery bridge.

The transport is newline-delimited JSON. Initial requests contain
`protocol_version`, `token`, and `command` (`send` or `watch`). Send requests also
contain `id` and `msg`. Successful replies include `ok: true` and the protocol
version. Watch replies have `event: "state"` and `message`, either null for idle
or an object containing `id`, `name`, and `msg`. Every subscription receives an
immediate snapshot. There are no polling heartbeats while connected and idle.

Limits: 8 KiB UTF-8 per message, 64 KiB per wire frame, 256 pending messages in
addition to the active one, and 128 concurrent connections. Initial requests,
connects, and writes have five-second deadlines. A slow watcher cannot block the
queue; overflowing its 16-update buffer disconnects it so it can resubscribe.
Incoming terminal escapes and invisible/control characters are stripped. Only
the renderer generates ANSI styling.

Clean shutdown removes discovery data while still holding the singleton lock.
A crash releases the lock; the next server replaces stale discovery data and
watchers reconnect. The active message and queue are **not durable**. Do not
automatically retry a failed send: a lost acknowledgement can leave acceptance
uncertain, and v1 has no delivery deduplication.

## tuios bottom dock

The listener and dock watcher are intentionally separate. Each tuios client runs
its own watcher; only the listener owns the queue and its display timing.

This dotfiles configuration adds the component alongside the existing center
components:

```toml
[dock]
center = ["custom/agent-comms", "windows"]

[dock.custom.agent-comms]
command = 'exec "$HOME/.local/bin/agent-comms" watch --width 80'
refresh = "push"
max-width = 80
```

The existing dock remains at the bottom. Each stdout line replaces the component
text; SGR colors survive, but cursor movement and clearing do not. This is not an
embedded terminal or a full-width/multi-row rail. Other components share the bar,
and tuios may truncate it further. This tuios version caps custom components at
80 cells, so the integration requests 80 even though standalone `watch` defaults
to 96. The quiet idle display is `[o_o] comms: idle`.

The initial setup runs `serve` in its own detached tuios session named
`agent-comms`. Detaching other tuios clients does not stop it. Inspect it with
`tuios attach agent-comms`. A tuios **daemon restart** ends its running process;
restart `serve` manually in that session after restoration. No OS login service,
startup hook, or automatic supervisor is installed in v1. Alternatively run
`agent-comms serve` in an independent terminal; the singleton lock prevents
accidental duplicate listeners.

On Windows, use a local command such as `py -3 C:/path/to/agent_comms.py watch` in
the dock table rather than copying the POSIX `exec` command above.

## Tests

```sh
python3 -m unittest discover -s agent-comms -v
```

From this directory, `python3 -m unittest -v` also works. Tests use isolated config,
state, discovery, and ephemeral ports; they do not touch the live dock/server.
