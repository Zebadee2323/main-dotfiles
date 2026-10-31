# Safe cache reuse across worktrees

Consider cache reuse when dependency setup or compilation is a material cost.
Inspect the repository's existing setup first; many tools already share a cache.
Avoid extra infrastructure when reuse is already effective or setup would cost
more than the work it saves. This policy does not authorize otherwise forbidden
builds, dependency downloads, changes to permissions, or global configuration.

## Establish a stack-specific plan

Identify the actual tool, version, build mode and cache directory. Consult its
installed help and current official documentation before enabling sharing;
record the relevant evidence once in the run artifacts. Similar directory names
or a successful single-process build do not establish concurrency safety.

Classify each candidate:

| Candidate | Default decision |
| --- | --- |
| Tool-managed downloaded package/archive store | Reuse if the tool documents integrity checks and concurrent access. |
| Content-addressed compiler/action cache | Reuse if keying, atomic publication and locking are documented for this setup. |
| Installed dependencies, virtual environments, generated files, object/output directories, incremental compiler databases or project/editor state | Keep per worktree unless the exact tool explicitly supports shared use and the conditions below are satisfied. |

For writable sharing, establish all of the following:

- The key captures the inputs relevant to correctness: source/dependency content,
  lockfiles, compiler/toolchain and plugins, target/architecture, flags, build
  mode and relevant environment. Use the tool's own keying where supported.
- Concurrent readers/writers are supported with appropriate locking and atomic
  publication; interrupted builds cannot expose partial entries as valid hits.
- Cache entries are portable across these worktree paths, or the tool explicitly
  handles absolute paths and checkout-specific metadata. Do not invent path
  remapping flags to force hits without checking debugging and correctness effects.
- Dependency installation or lifecycle hooks cannot modify shared package contents
  in ways that leak one task's state into another. Account for native modules and
  toolchain-specific artifacts, not just dependency version numbers.

If any condition is uncertain, keep that state isolated. Alternatives include a
supported read-only cache of immutable entries with local misses, or per-worktree
caches seeded using a documented copy/snapshot mechanism. Do not hardlink mutable
files. Serializing writers alone does not fix invalid keys or incompatible state.

## Prefer tool configuration; use symlinks deliberately

Prefer documented cache-root environment variables or configuration scoped to the
run. Use a symlink only when the tool supports the resulting layout and there is
a clear benefit over native configuration. A symlink does not provide locking,
immutability or isolation.

Before creating a link, verify its canonical target, ownership and intended scope.
Use an existing supported user cache, or a run-owned location outside disposable
worker checkouts that survives until all consumers finish. Never replace a tracked
path or delete/overwrite an existing directory to make a link fit. Preserve user
configuration. A new local cache link must remain outside the commit scope using
an appropriate local exclusion; do not modify tracked ignore rules just for the
experiment. Check Git status so `ship commit` cannot sweep the link into a commit.

Record cache path/configuration, compatibility scope, concurrency evidence,
owner, consumers, fallback and cleanup policy in the task ledger or a linked
artifact. Tell workers exactly which cache is shared and which outputs remain
local. Only the supervisor coordinates changes to the shared arrangement.

## Validate the setup with a worker

Delegate a small representative check to a worker before broad adoption. Confirm
that the configured path is actually used and that warm reuse saves useful time
or reports hits. Where practical, compare relevant outputs with an isolated-cache
run and check that changing an input invalidates the relevant result. Use existing
checks and keep the trial proportional to the risk. Documentation establishes
support; a passing smoke test alone does not prove concurrent safety.

Limit concurrent builds according to CPU, memory, disk and cache contention.
Separate worktrees do not make unlimited parallel compilation efficient. Workers
report timings/hits when observable, cache errors and unexpected writes; do not
invent savings. On suspected stale results or corruption, stop affected consumers,
fall back to isolated caches and rerun only the affected validation. Never clear a
shared user cache as a troubleshooting shortcut.

The source snapshot helper does not fingerprint ignored caches or external
symlink targets. Record toolchain, dependency/lockfile identity and cache mode
with test evidence; a matching Git snapshot alone cannot validate mutable shared
build state. Have the reviewer assess the cache assumptions and any uncertainty.

## Release and cleanup

Keep shared targets until every consumer has stopped. Remove only run-created
links/configuration and run-owned disposable caches that are no longer needed;
never follow symlinks recursively during cleanup or delete a pre-existing cache.
Fresh agent contexts may reuse a verified safe cache, but do not inherit another
task's mutable build outputs merely to avoid a cold build.
