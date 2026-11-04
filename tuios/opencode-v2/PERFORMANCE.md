# Compact TUIOS mirror (v0.3)

TUIOS gets a recent conversation view, **not a second full transcript**.
OpenCode remains the place to inspect full answers, reasoning, tool arguments,
output, patches, shell commands and older history.

## What is retained

- Up to **40 recent prompts/replies**, plus **8 recent tool-only steps** in that
  conversation window. Tool-only activity does not displace the user's prompt.
- At most **4 KiB of UTF-8 text per message**, shared across its text parts;
  truncation is explicitly marked. Attachment bodies/paths are not copied.
- At most **8 tool summaries per assistant message**: tool name, running/done/
  error status, and a single-line error of at most 256 bytes when needed.
- Minimal session/state/model/cost metadata and short permission/question labels.

Reasoning, raw tool inputs, tool output, diffs, native provider metadata and
image data are **not read by the projector**. Explicit shell commands become a
generic running/finished/failed label, not copied command text.

Encoded live records are capped at **256 KiB**. The append journal is compacted
before it would exceed approximately twice the live size plus **64 KiB** of
slack: at most about **576 KiB**, plus a small metadata header, per pane.
Temporary atomic replacements add one compact copy during a rewrite.

## Runtime costs

- Only changed records are appended, coalesced over **750 ms**. Unchanged text
  and changes exclusively to omitted content produce no writes.
- No idle polling or per-stream subprocesses. State and filesystem workers are
  independent, single-flight and coalesced/bounded under load.
- One persistent append handle and one reusable journal per pane. Cold loading
  yields every 32 projected messages and cancels on tab switches.
- Initialization/session switches write the compact selected history once.
  Cache/API synchronization and OpenCode's own storage are unaffected.
- Compaction occasionally rewrites the small retained view. TUIOS's own page
  parsing/rendering still has a cost and has not been benchmarked end to end.
- The journal is maintained even when the TUIOS conversation view is closed;
  the plugin has no reader-demand signal. Closed panes leave files behind;
  there is no background cleanup scan. Old prototype JSON files are not removed.
- Under reporting overload, intermediate telemetry can be coalesced, preserving
  latest state/recent turn-end rather than an unlimited history of transitions.
  The plugin never answers permissions or forms.

## Measurements

Local synthetic benchmark, macOS, Node **v26.12.1**, October 26, 2026. Source
messages contain 32 Ki-character text; the last message receives 100 growing
text updates. Each strategy runs in a fresh process. These measurements use
real encoding and count **logical bytes submitted**, with **no disk I/O**.

| Source history | Compact retained messages | Initial mirror | Writes across 100 updates |
|---|---:|---:|---:|
| 33 messages / ~1 MiB | 33 | 131.19 KiB | 121.44 KiB |
| 500 messages / ~15.64 MiB | 40 | 159.90 KiB | 121.50 KiB |

In both cases only 57 of the 100 updates produce writes: subsequent changes
beyond the 4 KiB display limit no longer change the compact projection.
Initial content and cumulative update writes are different quantities; the
initial mirror is not rewritten 100 times. CPU time for those 100 compact
updates was 1.96 ms / 1.60 ms respectively, excluding initialization. These are
microbenchmark figures, **not UI frame-time or real-workload guarantees**.

The benchmark also retains the original full-snapshot baseline for comparison.
Its policy keeps up to 500 messages and 32 Ki characters per text part. That is
intentionally **more content than the compact mirror**: current comparisons
measure both smaller content and incremental writes, not equal-content speed.
The previously reported ~313 MiB figure was repeated writes of the same large
synthetic history, not the size needed for a TUIOS conversation.

```sh
npm run benchmark
# Optional OS-buffered temporary-file writes; no fsync/physical SSD measurement:
npm run benchmark -- --disk
```

## Regression coverage

Tests verify omitted fields are not accessed, reasoning-only changes do not
write, UTF-8/message budgets, prompt retention through tool-heavy turns, small
live/disk caps, 10,000-event bursts, bounded reporting, slow I/O, private file
modes, partial-write recovery, compaction and tab-switch lifecycle ordering.
The live OpenCode/Sidekick → TUIOS conversation screen still needs an end-to-end
check after enabling the plugin.
