# Native Windows support status and remaining plan

The Python core already has Windows-specific paths, file locking, UTF-8 output,
a `.cmd` launcher, and native audio playback. The remaining work is mostly tuios
integration and native Windows verification. WSL remains a separate environment;
it does not validate native Windows support.

## Implemented: experimental per-user service management

The `service install/start/stop/restart/status/logs/uninstall` commands now select
a Windows Task Scheduler backend on native Windows. It registers a SID-scoped,
least-privilege interactive-token task at sign-in, with duplicate-start
protection, one-minute failure retries (up to 255), explicit paths, and no stored
password or elevation. A Python runner hosts the listener in the scheduled action
PID and captures early errors. Task operations use PowerShell/COM and JSON, not
localized `schtasks` text. See [README.md](README.md#experimental-windows-service)
for installation, stop semantics, logs, and a Windows tuios hook example.

**This backend is not native-Windows validated.** Portable tests exercise the
generated XML, quoting, ownership/path checks, readiness identity, supervisor
command selection, runner behavior, log following, and failure paths. These do
not establish that task registration or execution succeeds on Windows.

Before calling the service backend supported, verify on a standard Windows user:

- Install/start/status/logs/restart/stop/uninstall without an elevated shell.
- Sign out/in and confirm startup; confirm closing the agent/tuios does not stop it.
- Trigger a nonzero listener exit and verify the one-minute retry in task history.
- Confirm explicit stop disables recovery and logon startup, and start re-enables it.
- Test multiple simultaneous starts, different user accounts, unmanaged listeners,
  unrelated existing tasks, and custom paths with spaces/Unicode.
- Confirm early configuration failures reach `windows.stderr.log`, and following
  `server.log` does not block rotation.
- Confirm no leftover action process after stop/uninstall and safe stale discovery
  recovery after forced termination. Also test missing PowerShell, disabled Task
  Scheduler, and enterprise policy denial without bypasses.

## 1. Fix command execution in tuios

- Replace unconditional `sh -c` execution for dock components with a
  platform-aware command runner. Cover push watchers, polling, and click actions.
- Prefer an executable-plus-arguments option for commands that do not need a
  shell. Retain shell commands with explicit, documented platform semantics.
- Test executable paths and arguments containing spaces and Unicode.

This needs a change in tuios itself. Merely replacing the dock command with
`py -3 ...` still requires `sh` in the currently reviewed implementation.

## 2. Supply Windows installation and configuration

- Add a Windows setup script that checks for Python 3.11+, installs or exposes
  the launcher on PATH, and installs the requested harness adapters.
- Provide a Windows tuios configuration snippet for the dock watcher and both
  audio-control keybindings. Do not copy the POSIX `exec "$HOME/..."` commands.
- Generate correctly quoted commands using the installed interpreter and helper
  paths, rather than assuming a particular checkout location.
- Integrate the experimental scheduled-task startup commands with Windows setup;
  make opt-in service installation explicit. Tuios must request idempotent start
  rather than own or restart the listener process.
- Smoke-test the Codex CLI invocation and hook command on Windows, including
  common `.cmd` installations, and handle launcher resolution where necessary.

## 3. Clean up the whole Windows process tree

- In tuios, supervise dock commands with a Windows Job Object or equivalent
  process-tree lifecycle management, rather than killing only the shell.
- Ensure detach, dock refresh, configuration reload, and client exit stop the
  Python watcher and any audio-processing children.
- Verify that cancelled playback releases its WAV file before cleanup.

## 4. Coordinate history readers and writers

- Make history reads participate in the same cross-process locking protocol as
  writes, so Windows cannot reject replacement while a reader holds the file.
- Refactor the history helper to avoid acquiring the same lock recursively when
  a writer reads the existing records.
- Add a concurrent history-read/send regression test on Windows.

## 5. Add native Windows verification

- Add a Windows CI job running the Python and Node test suites.
- Replace the deferred native Windows hook test with actual launcher/hook tests.
- Cover lock contention, singleton enforcement, crash recovery, Unicode, paths
  with spaces, and history replacement.
- Test graceful Ctrl+C separately from forced termination; Windows process
  termination does not have POSIX SIGTERM cleanup semantics.
- Run an end-to-end tuios smoke test: start listener, attach watcher, resolve
  identity, send, hear speech, mute/unmute, detach, reload, and restart.
- Check captions still work without an API key or FFmpeg, and verify no watcher,
  FFmpeg process, or audio file is left behind after shutdown.

## Suggested order

1. Fix tuios command execution and process-tree cleanup.
2. Add Windows setup/configuration and fix the history locking race.
3. Enable Windows CI and complete the end-to-end smoke test.

Call native Windows supported only after those checks pass. Current passing
macOS tests are useful regression coverage, not Windows execution evidence.
