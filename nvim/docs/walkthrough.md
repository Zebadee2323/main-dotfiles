# Interactive walkthroughs

Enable once from your Neovim configuration (Neovim 0.11+):

```lua
require("naia.walkthrough").setup()
```

Setup is reloadable and preserves the existing `AIWalk*` commands, temporary
keymaps, browser, voice integration, and legacy YAML imports. Voice uses the
existing `AIVoiceInterrupt` / `AIVoiceStop` commands and playback User events;
without voice, beats advance using the configured inter-step delays.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `nvim_walkthrough_start` | Validate a tour and request confirmation without blocking; auto-starts return its ID, revision, and step IDs. |
| `nvim_walkthrough_current_context` | Get current step, playback state, and mutation targets. |
| `nvim_walkthrough_insert_steps` | Append a child exploration without removing existing branches or starting playback. |
| `nvim_walkthrough_edit` | Explicit `append_children`, `replace_children`, `update_step`, or `remove_branch`. |
| `nvim_walkthrough_control` | Explicit `pause`, `resume`, `stop`, or `jump` to a stable step ID. |
| `nvim_walkthrough_export` | Write the current/last tour to a portable file (`path`, optional `root`, `overwrite`). |
| `nvim_walkthrough_load` | Load a file (`path`, optional `root`, `pause_after_step`, `confirm_start`). |
| `nvim_walkthrough_file_spec` | Get the JSON Schema, authoring rules, and an example for building files directly. |

These are the names advertised by upstream mcp-tools. The old ai_ tool prefix
is removed; editor commands, timing settings and portable files are unchanged.

`nvim_walkthrough_start` accepts `confirm_start` (default `true`). A non-modal
confirmation float lets the user start when ready. The MCP call returns
`{ "status": "awaiting_confirmation", "total_steps": N }` immediately, before
the user responds, so the agent is not held waiting. Enter/y starts; Esc/n/q or
closing the float cancels without replacing the existing tour. Fetch
`nvim_walkthrough_current_context` after the user starts to get the new tour's ID,
revision, and step IDs; a pending response does not identify an active tour.
New start requests replace any previous pending confirmation, and stopping
clears it. File loads use the same non-blocking confirmation.
Set `confirm_start = false` to start
immediately **only when the user explicitly requests auto-start or skipping
confirmation**. This is independent of `pause_after_step`.

Context accepts `include_tree`, `include_buffer`, and `include_editor` (all default
to false). Tree context includes ancestors, siblings, and up to five upcoming
steps. Buffer context reads the **live buffer**, including unsaved changes, with
`context_lines` padding (default 5, maximum 100), capped at 500 lines. Editor
context includes cursor, mode, and an active visual selection's line text and
column endpoints (selection text is capped at 500 lines).

Every edit requires `walkthrough_id` and `expected_revision` from a recent context
response. Edits increment the content revision; playback/navigation do not.
Mutations are validated before committing, return added/updated step IDs, and do
not start playback. Append preserves earlier children; replace explicitly removes
all descendants. The active step cannot be removed: navigate elsewhere first.
`update_step` supplies a complete replacement step while retaining its ID/parent.

Control additionally requires `expected_current_step_id` and
`expected_playback_revision` from context. Navigation and beat changes advance
the playback revision, preventing a delayed request from taking over even if the
user has navigated away and back to the same step. Stale requests fail with a
message to fetch current context again. Edits may optionally use these additional
guards too.

Example follow-up workflow:

1. Fetch context with `include_tree = true`.
2. Call `nvim_walkthrough_insert_steps` with the returned `walkthrough_id`,
   `expected_revision = revision`, explicit `parent_step_id`, and new `steps`.
3. Fetch fresh context and, if playback is requested, call `nvim_walkthrough_control`
   with `action = "jump"` and one of the returned step IDs.

**Migration:** the insert tool now requires explicit identity/revision/parent
fields. It no longer replaces descendants or automatically plays its additions.
Basic start payloads and the existing user-facing playback commands still work.
MCP clients should reconnect after setup/reload to discover the updated schemas.

## Locations and stable anchors

Every step still accepts `path`, 1-based inclusive `line_start` / `line_end`, and
`description`. Paths may be absolute or relative to the editor/repository roots.
All validated locations receive extmarks, including unvisited steps, so line
positions follow buffer edits during the tour.

Optionally specify one of:

```json
{"anchor": {"snippet": "queue.push(request)", "before": "validate(request)"}}
```

```json
{"anchor": {"symbol": "process_request"}}
```

Snippets match complete lines exactly. Optional `before` / `after` strings match
adjacent complete lines and disambiguate repeated code. Symbol anchors try LSP
document symbols, then Tree-sitter declarations with a `name` field; an attached
LSP or installed parser is needed. Missing or ambiguous anchors fail rather than
falling back to unrelated lines. Deleted anchors pause playback; context reports
`location_error` so an agent can repair the step or stop the tour.

## Narration beats and labels

A step may have `beats`, each with its own `description` and optional location.
Omitted location fields inherit the parent step's values. Each beat's focused
code is displayed before its narration starts. Voice completion advances the
next beat; `pause_after_step` pauses after the last beat, not between beats.
Manual next/previous/jump navigate whole steps.

Every code pane has a persistent file header showing a highlighted filename,
its path relative to the current working directory (home-shortened outside it),
and the focused line range. Comparison headers also identify **Before / Current**
alongside any custom label; snapshots show their Git revision or **Snapshot**.
Headers update with each step/beat, remain visible while paused or on the final
view, and restore your original window header when the walkthrough stops.

Focused ranges that fit in the viewport are centered on their midpoint using
Neovim's native `zz` scrolling, including near the end of the file. Folds hiding
the focus are opened. Taller ranges start at the top of the viewport instead.

Narration floats avoid the explained range and labeled highlights on plain steps
as well as comparisons, using a clear gap (or a shorter box) when space allows.
Comparison panes split only the walkthrough's source window; creating and closing
them preserves unrelated split sizes instead of equalizing the user's layout.
Snapshot cleanup detaches temporary buffers before wiping them, preserving the
source split itself when moving between snapshot steps.

```json
{
  "path": "src/queue.lua",
  "line_start": 10,
  "line_end": 20,
  "description": "Validation and dispatch",
  "beats": [
    {"line_start": 10, "line_end": 12, "description": "First validate the request."},
    {"line_start": 18, "line_end": 20, "description": "Then enqueue it."}
  ]
}
```

Steps and beats may also have `highlights`: an array of locations with optional
`label` text, for multiple labeled ranges in the focused file. During a comparison,
highlights must target one of the two visible buffers, not a third explanation
file. Use comparison panes to show different files together.

## Comparisons

A step or beat can contain `comparison = { left, right, diff? }`. Each side is a
location with an optional label. Live file locations show caller/callee or
implementation/test pairs. Add `revision` for a committed Git snapshot, or `text`
for a proposed snapshot. Snapshot ranges refer to the snapshot's lines, not the
working file's lines. Snapshot buffers are scratch, nonmodifiable buffers; no
proposed change is applied to source files.

```json
{
  "left": {"path": "src/queue.lua", "line_start": 10, "line_end": 20,
           "revision": "HEAD", "label": "Committed"},
  "right": {"path": "src/queue.lua", "line_start": 10, "line_end": 24,
            "label": "Working buffer"},
  "diff": true
}
```

Comparisons show **before (`left`) on the left and current (`right`) on the right**,
like a GitHub side-by-side diff. The current pane reuses the walkthrough's display
window; only the before code pane is added. There is no third explanation-code
view. Focus, cursor, and buffer context follow the current/right location.

Narration uses the original **floating box over the current/right pane**, with
no bottom narration split consuming code height. On each displayed comparison
step/beat, its vertical position is chosen from visible screen rows on **both**
sides, avoiding changed lines, deletion filler, the explained ranges, and labeled
highlights with one row of padding. Placement includes the box's border and
excludes window titles/status lines. It prefers a full-size clear gap, then the
largest clear shortened box. Shortened text ends with an ellipsis; spoken
narration is unchanged. If no clear gap fits, it minimizes overlap, prioritizing
the explained code over other changes. Dense diffs can still require overlap.
Calculating placement never scrolls or resizes either pane. Ordinary single-file
steps retain their original near-the-highlight placement.
Walkthrough diffs start unfolded on both sides, recompute diff filler, and use
diff-aware centering so independently collapsed folds cannot offset matching
code vertically. Your original folding settings are restored afterward.

When a step/beat changes or playback stops, the before pane and narration box
are closed, scratch snapshots are deleted, and the borrowed display window's
buffer, view, diff, folding, binding, wrapping, and title options are restored.
Unsaved source changes are preserved, including with `hidden` disabled. Natural
completion retains the final view until the walkthrough is stopped or replaced.
Existing unrelated editor windows remain in place. Snapshot reads use local Git
only; they do not fetch revisions or run arbitrary shell commands.

## Persistence

`AIWalkExport <path>` writes plain UTF-8 version-3 JSON with all beats, anchors,
labels, comparison snapshots, and tree identities. Existing files are protected;
use `AIWalkExport! <path>` to overwrite. `AIWalkImport <path>` (or `AIWalkLoad`)
loads the file and confirms before starting playback. MCP load also confirms by
default; only explicitly requested auto-starts should set `confirm_start=false`.
Cancelling or invalid input preserves the current tour.

All persisted location paths are relative to the current Git repository root
(cwd outside Git), **not the walkthrough file's directory**. Open the recipient's
checkout before loading. MCP export/load accept an explicit `root` for other
workspaces. This root is never stored in the file. Paths in steps, beats,
highlights, and comparison panes use forward slashes; absolute paths, home paths,
`.`/`..` components, and symlinks escaping the root are rejected. Export fails
rather than exposing locations outside that root. Runtime fields and arbitrary
metadata are omitted. Unsaved source edits are not embedded automatically.

```json
{"version":3,"steps":[{"path":"src/main.lua","line_start":1,"line_end":3,"description":"The entry point."}]}
```

Agents should call `nvim_walkthrough_file_spec` before authoring a file. Its schema
and semantic rules cover optional IDs/parents, inherited beat locations, anchors,
highlights and comparisons. Loading validates the entire tour before replacement.
Unknown fields and unsupported versions are rejected in version-3 files.

Legacy version-2 JSON (including the old `# ai-walkthrough` header) and YAML
remain supported for **trusted local use**; these may contain absolute paths and
do not have the version-3 containment guarantees. Re-export them to make portable
files. Narration, labels, anchor snippets, and explicitly supplied snapshot text
remain verbatim content: review them for private information before sharing.
