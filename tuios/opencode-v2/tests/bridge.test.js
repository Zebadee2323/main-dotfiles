import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtemp, mkdir, readFile, writeFile, stat, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { Bridge, transcriptPath, commandRunner } from "../bridge.js";
import { Journal, JournalFile, recordsFor, text, MAX_MESSAGES, MAX_TOOL_MESSAGES, MAX_LIVE_BYTES, MAX_TEXT_BYTES, COMPACT_SLACK } from "../transcript.js";
import { Reporter } from "../reporter.js";
import { errorSummary, writeDiagnostic } from "../diagnostics.js";

const env = { TUIOS_PANE_ID: "pane-a", TUIOS_SESSION: "test", XDG_STATE_HOME: "/state" };
const tick = () => new Promise((resolve) => setImmediate(resolve));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const assistant = { id: "msg1", type: "assistant", time: { created: 10 }, content: [{ type: "text", text: "Hello" }] };
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };
async function temporary(prefix) {
  const root = join(tmpdir(), "opencode");
  await mkdir(root, { recursive: true });
  return mkdtemp(join(root, prefix));
}

class MemoryFile {
  data = "";
  writes = [];
  async replace(data) { this.data = data; this.writes.push({ replace: true, data }); }
  async append(data) { this.data += data; this.writes.push({ replace: false, data }); }
  async close() { this.closed = true; }
  records() {
    const latest = new Map();
    for (const line of this.data.trim().split("\n")) {
      const record = JSON.parse(line);
      if (record.id) latest.set(record.id, record);
    }
    return [...latest.values()];
  }
}

function fixture(options = {}) {
  const calls = [], files = [], journals = [], warnings = [], diagnostics = [];
  const messages = options.messages ?? [structuredClone(assistant)];
  const sessions = {
    sync: async () => {}, get: (id) => ({ id, model: { id: "model" }, cost: 0.12 }), status: () => "idle",
    message: { sync: async () => {}, list: () => messages, get: (_id, mid) => messages.find((m) => m.id === mid) },
    permission: { sync: async () => {}, list: () => [] }, form: { sync: async () => {}, list: () => [] }, ...options.sessions,
  };
  const ctx = { data: { session: sessions }, ui: { toast: { show: (w) => warnings.push(w) } } };
  const bridge = new Bridge(ctx, { env, interval: 5, diagnose: async (path, stage, error) => diagnostics.push({ path, stage, ...errorSummary(error) }),
    run: async (args, input) => calls.push({ args, input }),
    journal: (path, id) => {
      const file = new MemoryFile(); files.push(file);
      if (options.failWrites) file.replace = async () => { throw new Error("disk full"); };
      const journal = new Journal(path, id, { file }); journals.push(journal); return journal;
    }, ...options.dependencies });
  const event = (type, data = {}) => bridge.event({ type, data: { sessionID: "ses_A", ...data } });
  const states = () => calls.filter((c) => c.args[0] === "set-agent-state").map((c) => c.args[1])
    .filter((s, i, a) => i === 0 || s !== a[i - 1]);
  const settle = async () => { await tick(); await bridge.idle(); await tick(); await bridge.idle(); };
  return { bridge, calls, files, journals, messages, warnings, diagnostics, event, states, settle };
}

test("hydrates a resumed transcript and reports exact path and original identity", async () => {
  const f = fixture(); f.bridge.select("ses_A"); await f.settle();
  assert.equal(f.files[0].records()[0].content[0].text, "Hello");
  const report = f.calls.find((c) => c.args.includes("--transcript-path")).args;
  assert.equal(report[report.indexOf("--agent-session-id") + 1], "ses_A");
  assert.equal(report[report.indexOf("--transcript-path") + 1], transcriptPath(env, "ses_A"));
  assert.equal(report[report.indexOf("--window") + 1], "pane-a");
  await f.bridge.close();
});

test("preserves ordinary same-batch execution order and ignores other sessions", async () => {
  const f = fixture(); f.bridge.select("ses_A"); await f.settle();
  f.event("session.execution.started"); f.event("session.execution.succeeded"); f.event("session.execution.started");
  f.event("session.execution.failed", { sessionID: "ses_CHILD" }); await f.settle();
  assert.deepEqual(f.states(), ["none", "idle", "working", "done", "working"]);
  await f.bridge.close();
});

test("multiple blockers stay blocked until all resolved; never replies", async () => {
  const f = fixture(); f.bridge.select("ses_A"); await f.settle();
  f.event("session.execution.started"); f.event("permission.asked", { id: "p1", action: "shell" });
  f.event("permission.asked", { id: "p2", action: "edit" }); f.event("permission.replied", { requestID: "p1" });
  await f.settle(); assert.equal(f.states().at(-1), "needs_input");
  f.event("form.created", { form: { id: "f1", sessionID: "ses_A", title: "Which branch?" } });
  f.event("permission.replied", { requestID: "p2" }); await f.settle();
  const last = f.calls.filter((c) => c.args[0] === "set-agent-state").at(-1).args;
  assert.equal(last[last.indexOf("--kind") + 1], "question");
  f.event("form.cancelled", { id: "f1" }); await f.settle();
  assert.equal(f.states().at(-1), "working");
  assert.ok(f.calls.every((c) => ["set-agent-state", "agent-statusline"].includes(c.args[0])));
  await f.bridge.close();
});

test("interrupt is idle; failure survives compatibility idle", async () => {
  const f = fixture(); f.bridge.select("ses_A"); await f.settle();
  f.event("session.execution.interrupted"); await f.settle(); assert.equal(f.states().at(-1), "idle");
  f.event("session.execution.failed", { error: { message: "Unavailable" } });
  f.event("session.status", { status: { type: "idle" } }); await f.settle();
  assert.equal(f.states().at(-1), "errored"); await f.bridge.close();
});

test("switch during hydration never writes old conversation", async () => {
  const gate = deferred();
  const f = fixture({ sessions: { sync: (id) => id === "ses_A" ? gate.promise : Promise.resolve() } });
  f.bridge.select("ses_A"); f.bridge.select("ses_B"); await f.settle();
  gate.resolve(); await f.settle();
  assert.equal(f.files[0].writes.length, 0); assert.ok(f.files[0].closed);
  assert.equal(JSON.parse(f.files[1].data.split("\n")[0]).sessionId, "ses_B");
  await f.bridge.close();
});

test("live state wins over late hydration including waiting permission", async () => {
  const gate = deferred(); const f = fixture({ sessions: { sync: () => gate.promise } });
  f.bridge.select("ses_A"); f.event("session.execution.started");
  f.event("permission.asked", { id: "p", action: "shell" }); gate.resolve(); await f.settle();
  assert.equal(f.states().at(-1), "needs_input"); await f.bridge.close();
});

test("write failures don't block state reporting and warn once", async () => {
  const f = fixture({ failWrites: true }); f.bridge.select("ses_A"); await f.settle();
  f.event("session.execution.started"); await f.settle();
  assert.equal(f.states().at(-1), "working"); assert.equal(f.warnings.length, 1);
  assert.ok(f.calls.every((c) => !c.args.includes("--transcript-path"))); await f.bridge.close();
});

test("a rejected optional form sync doesn't prevent startup or live mirroring", async () => {
  const f = fixture({ sessions: { form: {
    sync: async () => { throw Object.assign(new Error("PRIVATE response"), { status: 503 }); }, list: () => [],
  } } });
  f.bridge.select("ses_A"); await f.settle();
  assert.ok(f.bridge.current.ready); assert.equal(f.files[0].records()[0].content[0].text, "Hello");
  assert.match(f.warnings[0].message, /form-sync unavailable \(503\); using cached\/live data/);
  assert.equal(f.diagnostics[0].stage, "form-sync");
  f.messages[0].content[0].text = "Still works";
  f.event("session.message.content.updated", { messageID: "msg1" }); await wait(20); await f.settle();
  assert.equal(f.files[0].records()[0].content[0].text, "Still works"); await f.bridge.close();
});

test("an optional sync which hangs doesn't gate the initial transcript", async () => {
  const gate = deferred();
  const f = fixture({ sessions: { permission: { sync: () => gate.promise, list: () => [] } } });
  f.bridge.select("ses_A"); await f.settle();
  assert.equal(f.files[0].records()[0].content[0].text, "Hello");
  await f.bridge.close(); gate.resolve(); await tick(); assert.equal(f.warnings.length, 0);
});

test("new-session not-found refreshes retry and hydrate without warnings", async () => {
  const counts = new Map();
  const refresh = (stage) => async () => {
    const count = (counts.get(stage) ?? 0) + 1; counts.set(stage, count);
    if (count < 3) throw { _tag: "SessionNotFoundError" };
  };
  const f = fixture({ dependencies: { retryDelays: [1, 1] }, sessions: {
    sync: refresh("session"),
    message: { sync: refresh("history"), list: () => [assistant] },
    permission: { sync: refresh("permission"), list: () => [] },
    form: { sync: refresh("form"), list: () => [{ id: "f1", title: "Which branch?" }] },
  } });
  f.bridge.select("ses_A"); await wait(30); await f.settle();
  assert.deepEqual([...counts.values()], [3, 3, 3, 3]);
  assert.equal(f.files[0].records()[0].content[0].text, "Hello");
  assert.equal(f.states().at(-1), "needs_input");
  assert.equal(f.warnings.length, 0); assert.equal(f.diagnostics.length, 0);
  await f.bridge.close();
});

test("persistent not-found warns after a bounded retry budget without blocking startup", async () => {
  let attempts = 0;
  const f = fixture({ dependencies: { retryDelays: [1, 1] }, sessions: { form: {
    sync: async () => { attempts++; throw Object.assign(new Error(), { name: "SessionNotFoundError" }); },
    list: () => [],
  } } });
  f.bridge.select("ses_A"); await f.settle();
  assert.ok(f.bridge.current.ready);
  await wait(30); await f.settle();
  assert.equal(attempts, 3); assert.equal(f.warnings.length, 1);
  assert.match(f.warnings[0].message, /form-sync unavailable \(SessionNotFoundError\)/);
  assert.equal(f.diagnostics[0].stage, "form-sync");
  await f.bridge.close();
});

test("switching sessions or closing cancels pending not-found retries", async () => {
  const attempts = [];
  const f = fixture({ dependencies: { retryDelays: [1000] }, sessions: {
    sync: async (id) => { attempts.push(id); throw { _tag: "SessionNotFoundError" }; },
  } });
  f.bridge.select("ses_A"); await tick();
  const first = f.bridge.current;
  assert.equal(first.retryWaiters.size, 1);
  f.bridge.select("ses_B"); await tick();
  assert.equal(first.retryWaiters.size, 0);
  const second = f.bridge.current;
  assert.equal(second.retryWaiters.size, 1);
  await f.bridge.close(); await tick();
  assert.equal(second.retryWaiters.size, 0);
  assert.deepEqual(attempts, ["ses_A", "ses_B"]);
  assert.equal(f.warnings.length, 0); assert.equal(f.files[0].writes.length, 0);
});

test("live blocker replies win over delayed not-found retry hydration", async () => {
  let attempts = 0;
  const f = fixture({ dependencies: { retryDelays: [1] }, sessions: { form: {
    sync: async () => { if (++attempts === 1) throw { _tag: "SessionNotFoundError" }; },
    list: () => [{ id: "f1", title: "Stale question" }],
  } } });
  f.bridge.select("ses_A"); await tick();
  f.event("form.replied", { id: "f1" });
  await wait(20); await f.settle();
  assert.equal(attempts, 2); assert.equal(f.bridge.current.forms.size, 0);
  assert.equal(f.states().at(-1), "idle"); assert.equal(f.warnings.length, 0);
  await f.bridge.close();
});

test("a failed history refresh keeps cached data and live events usable", async () => {
  const messages = [structuredClone(assistant)];
  const f = fixture({ messages, sessions: { message: {
    sync: async () => { throw Object.assign(new Error("unavailable"), { code: "ECONNRESET" }); },
    list: () => messages, get: (_id, mid) => messages.find((m) => m.id === mid),
  } } });
  f.bridge.select("ses_A"); await f.settle();
  assert.ok(f.bridge.current.ready); assert.equal(f.files[0].records()[0].content[0].text, "Hello");
  assert.equal(f.diagnostics[0].code, "ECONNRESET");
  f.event("session.execution.started"); await f.settle(); assert.equal(f.states().at(-1), "working");
  await f.bridge.close();
});

test("a late failure from the previous session causes no false mirror warning", async () => {
  let reject;
  const failed = new Promise((_, fail) => { reject = fail; });
  const f = fixture({ sessions: { sync: (id) => id === "ses_A" ? failed : Promise.resolve() } });
  f.bridge.select("ses_A"); await tick(); f.bridge.select("ses_B"); await f.settle();
  reject(new Error("old request failed")); await f.settle();
  assert.equal(f.warnings.length, 0); assert.equal(f.diagnostics.length, 0);
  assert.equal(JSON.parse(f.files[1].data.split("\n")[0]).sessionId, "ses_B"); await f.bridge.close();
});

test("expected cancellation doesn't show a misleading failure", async () => {
  const f = fixture({ sessions: { sync: async () => { throw Object.assign(new Error("canceled"), { name: "AbortError" }); } } });
  f.bridge.select("ses_A"); await f.settle();
  assert.equal(f.warnings.length, 0); assert.equal(f.files[0].records()[0].content[0].text, "Hello");
  await f.bridge.close();
});

test("diagnostics are private and omit messages, complete stacks and URLs", async () => {
  const dir = await temporary("tuios-v2-diagnostics-");
  try {
    const path = join(dir, "pane.jsonl");
    const error = Object.assign(new Error("PRIVATE_TOKEN https://user:password@server/prompt"), { code: "EACCES" });
    error.stack = "Error: PRIVATE_TOKEN\n at run (/some/private/path/transcript.js:100:5)";
    await writeDiagnostic(path, "write", error);
    const data = await readFile(`${path}.diagnostic.json`, "utf8");
    const value = JSON.parse(data);
    assert.equal(value.code, "EACCES"); assert.equal(value.frame, "transcript.js:100:5");
    assert.equal(value.stage, "write"); assert.ok(data.length < 400);
    assert.ok(!data.includes("PRIVATE_TOKEN")); assert.ok(!data.includes("password")); assert.ok(!data.includes("/some/private"));
    assert.equal((await stat(`${path}.diagnostic.json`)).mode & 0o777, 0o600);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("one file per pane, distinct panes, and traversal rejection", () => {
  assert.notEqual(transcriptPath(env, "ses_A"), transcriptPath({ ...env, TUIOS_PANE_ID: "b" }, "ses_A"));
  assert.equal(transcriptPath(env, "ses_A"), transcriptPath(env, "ses_B"));
  assert.throws(() => transcriptPath(env, "../../secret"));
});

test("keeps text and tool status/errors, omitting reasoning, inputs, results and diffs", () => {
  const records = recordsFor({ ...assistant, native: { secret: "provider-secret" }, content: [
    { type: "text", text: "Hello" }, { type: "reasoning", text: "Think" },
    { type: "tool", id: "t1", name: "shell", state: { status: "completed", input: { command: "pwd" }, content: [{ type: "text", text: "/repo" }] } },
    { type: "tool", id: "t2", name: "edit", state: { status: "error", input: {}, error: { message: "Denied" } } },
    { type: "tool", id: "t3", name: "edit", state: { status: "completed", metadata: { files: [{ file: "a", patch: "@@ -1 +1 @@\n-a\n+b" }] } } },
    { type: "tool", id: "t4", name: "edit", state: { status: "streaming" } },
  ] });
  assert.equal(records.length, 5); assert.equal(records[0].content[0].text, "Hello");
  assert.equal(records[1].toolCalls[0].result, ""); assert.equal(records[2].toolCalls[0].status, "error");
  assert.equal(records[2].toolCalls[0].result, "Denied");
  assert.ok(records.slice(1).every((r) => !Object.hasOwn(r.toolCalls[0], "args") && !Object.hasOwn(r.toolCalls[0], "resultDisplay")));
  assert.equal(records[4].toolCalls[0].status, "executing");
  assert.ok(!JSON.stringify(records).includes("Think"));
  assert.ok(!JSON.stringify(records).includes("/repo"));
  assert.ok(!JSON.stringify(records).includes("pwd"));
  assert.ok(!JSON.stringify(records).includes("provider-secret"));
  assert.deepEqual(recordsFor({ id: "system", type: "system", text: "private" }), []);
});

test("journal appends only changed parts; unchanged and reverted updates are no-ops", async () => {
  const file = new MemoryFile(), journal = new Journal("unused", "ses_A", { file });
  const message = structuredClone(assistant);
  journal.update(message); await journal.flush(); const original = file.data;
  journal.update(message); await journal.flush(); assert.equal(file.writes.length, 1);
  message.content[0].text = "new"; journal.update(message); journal.update(message); await journal.flush();
  assert.equal(file.writes.length, 2); assert.ok(file.data.startsWith(original));
  assert.equal(file.records().length, 1); assert.equal(file.records()[0].content[0].text, "new");
  message.content[0].text = "temporary"; journal.update(message);
  message.content[0].text = "new"; journal.update(message); await journal.flush();
  assert.equal(file.writes.length, 2);
});

test("long history isn't re-encoded or rewritten when one message changes", async () => {
  const file = new MemoryFile(), journal = new Journal("unused", "ses_A", { file });
  const history = Array.from({ length: 400 }, (_, i) => ({ ...assistant, id: `m${i}`, content: [{ type: "text", text: "x".repeat(8192) }] }));
  journal.reconcile(history); await journal.flush();
  const before = journal.stats.encodedRecords;
  const changed = { ...history.at(-1), content: [{ type: "text", text: "changed" }] };
  journal.update(changed); await journal.flush();
  assert.equal(journal.stats.encodedRecords - before, 1);
  assert.ok(Buffer.byteLength(file.writes.at(-1).data) < 200);
  assert.equal(journal.stats.replacements, 1);
  journal.reconcile(history); assert.equal(journal.stats.encodedRecords, before + 1);
});

test("compaction bounds stale revisions and rotates cursor identity", async () => {
  const file = new MemoryFile(), journal = new Journal("unused", "ses_A", { file, compactSlack: 100 });
  const message = structuredClone(assistant); journal.update(message); await journal.flush();
  const header = file.data.split("\n")[0];
  for (let i = 0; i < 20; i++) { message.content[0].text = `text${i}`; journal.update(message); await journal.flush(); }
  assert.ok(journal.stats.replacements > 1); assert.notEqual(file.data.split("\n")[0], header);
  assert.equal(file.records()[0].content[0].text, "text19");
});

test("byte-retention eviction doesn't repeatedly re-project omitted history", async () => {
  const file = new MemoryFile(), journal = new Journal("unused", "ses_A", { file, maxBytes: 700 });
  const history = Array.from({ length: 100 }, (_, i) => ({ ...assistant, id: `m${i}` }));
  journal.reconcile(history); await journal.flush();
  const encodes = journal.stats.encodedRecords, writes = file.writes.length;
  assert.ok(journal.messages.size < history.length);
  journal.reconcile(history); await journal.flush();
  assert.equal(journal.stats.encodedRecords, encodes); assert.equal(file.writes.length, writes);
});

test("removed parts, message eviction and explicit history changes compact cleanly", async () => {
  const file = new MemoryFile(), journal = new Journal("unused", "ses_A", { file });
  const message = { ...assistant, content: [{ type: "text", text: "a" }, { type: "text", text: "b" }] };
  journal.update(message); await journal.flush(); message.content.pop(); journal.update(message); await journal.flush();
  assert.equal(file.records().length, 1);
  const many = Array.from({ length: MAX_MESSAGES + 10 }, (_, i) => ({ ...assistant, id: `m${i}` }));
  journal.reconcile(many); await journal.flush(); assert.equal(journal.messages.size, MAX_MESSAGES);
  journal.reconcile(many.slice(-2)); await journal.flush(); assert.equal(file.records().length, 2);
});

test("projection never reads raw tool arguments, output, patches or reasoning text", () => {
  const state = { status: "completed" };
  for (const key of ["input", "content", "metadata"]) Object.defineProperty(state, key, { get: () => assert.fail(`read ${key}`) });
  const reasoning = { type: "reasoning", get text() { assert.fail("read reasoning"); } };
  const record = recordsFor({ ...assistant, content: [reasoning, { type: "tool", name: "custom", id: "t", state }] });
  assert.ok(JSON.stringify(record).length < 300);
  assert.equal(record.length, 1);
});

test("text is limited to 4 KiB per message, not per content part, and marked", () => {
  const records = recordsFor({ ...assistant, content: Array.from({ length: 20 }, () => ({ type: "text", text: "x".repeat(2048) })) });
  const contents = records.map((r) => r.content[0].text);
  assert.ok(contents.at(-1).endsWith("… [truncated]"));
  assert.ok(contents.reduce((n, s) => n + Buffer.byteLength(s), 0) <= MAX_TEXT_BYTES);
  const unicode = text("😀".repeat(10000));
  assert.ok(Buffer.byteLength(unicode) <= MAX_TEXT_BYTES); assert.ok(!unicode.includes("�"));
  assert.ok(unicode.endsWith("… [truncated]"));
});

test("tool-heavy turns keep the user's prompt and only recent tool-only steps", async () => {
  const file = new MemoryFile(), journal = new Journal("unused", "ses_A", { file });
  const prompt = { id: "prompt", type: "user", text: "Fix this bug", time: { created: 0 } };
  const tools = Array.from({ length: 200 }, (_, i) => ({ ...assistant, id: `tool${i}`, content: [{ type: "tool", id: `t${i}`, name: "read", state: { status: "completed" } }] }));
  journal.reconcile([prompt, ...tools, assistant]); await journal.flush();
  assert.ok(journal.messages.has("prompt")); assert.ok(journal.messages.has("msg1"));
  assert.equal(journal.toolMessages.size, MAX_TOOL_MESSAGES);
  assert.equal(file.records().length, 2 + MAX_TOOL_MESSAGES);
});

test("only eight recent tool summaries per step; no shell command or attachment payload", () => {
  const content = Array.from({ length: 100 }, (_, i) => ({ type: "tool", id: `t${i}`, name: "read", state: { status: "completed" } }));
  const records = recordsFor({ ...assistant, content });
  assert.equal(records.length, 8); assert.equal(records[0].toolCalls[0].id, "msg1:92");
  const shell = recordsFor({ id: "sh", type: "shell", get command() { assert.fail("raw command"); } });
  assert.equal(shell[0].content, "[Shell command]");
  const prompt = recordsFor({ id: "u", type: "user", text: "Image", files: [{ mime: "image/png", get data() { assert.fail("image payload"); } }] });
  assert.deepEqual(prompt[0].content[1], { fileData: {} });
});

test("default live and on-disk budgets are small even under repeated updates", async () => {
  const file = new MemoryFile(), journal = new Journal("unused", "ses_A", { file });
  const history = Array.from({ length: 500 }, (_, i) => ({ ...assistant, id: `m${i}`, content: [{ type: "text", text: "x".repeat(32768) }] }));
  journal.reconcile(history); await journal.flush();
  assert.ok(journal.liveBytes <= MAX_LIVE_BYTES); assert.equal(journal.messages.size, MAX_MESSAGES);
  for (let i = 0; i < 100; i++) {
    history.at(-1).content[0].text = String(i) + "x".repeat(32768);
    journal.update(history.at(-1)); await journal.flush();
    assert.ok(journal.diskBytes <= 2 * MAX_LIVE_BYTES + COMPACT_SLACK + 256);
  }
  assert.ok(Buffer.byteLength(file.data) < 600 * 1024);
});

test("cold history loading yields and cancels when the session is no longer active", async () => {
  const journal = new Journal("unused", "ses_A", { file: new MemoryFile() });
  const history = Array.from({ length: 500 }, (_, i) => ({ ...assistant, id: `m${i}` }));
  let active = true, yielded = false;
  setImmediate(() => { yielded = true; active = false; });
  await journal.reconcileAsync(history, new Set(), () => active);
  assert.ok(yielded); assert.equal(journal.messages.size, 32); assert.equal(journal.stats.bytes, 0);
});

test("partial append errors force a clean replacement on next flush", async () => {
  const file = new MemoryFile(), journal = new Journal("unused", "ses_A", { file });
  const message = structuredClone(assistant); journal.update(message); await journal.flush();
  file.append = async (data) => { file.data += data.slice(0, 10); throw new Error("full"); };
  message.content[0].text = "fixed"; journal.update(message); await assert.rejects(journal.flush());
  await journal.flush(); assert.equal(file.records()[0].content[0].text, "fixed");
});

test("private files append in-place; compaction is atomic and handle closes", async () => {
  const dir = await temporary("tuios-v2-test-");
  try {
    const path = join(dir, "nested", "session.jsonl"), file = new JournalFile(path);
    await file.replace('first\n'); const inode = (await stat(path)).ino;
    await file.append('second\n'); assert.equal((await stat(path)).ino, inode);
    assert.equal(await readFile(path, "utf8"), "first\nsecond\n");
    assert.equal((await stat(path)).mode & 0o777, 0o600);
    assert.equal((await stat(join(dir, "nested"))).mode & 0o777, 0o700);
    await file.replace('replacement\n'); assert.notEqual((await stat(path)).ino, inode);
    await file.close(); assert.equal(file.handle, undefined);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("stream bursts coalesce into one changed-message append and zero subprocesses", async () => {
  const f = fixture(); f.bridge.select("ses_A"); await f.settle();
  const calls = f.calls.length, writes = f.files[0].writes.length;
  for (let i = 0; i < 10000; i++) {
    f.messages[0].content[0].text = `Hello${i}`;
    f.event("session.message.content.updated", { messageID: "msg1" });
  }
  await wait(20); await f.settle();
  assert.equal(f.files[0].writes.length - writes, 1); assert.equal(f.calls.length, calls);
  assert.equal(f.files[0].records()[0].content[0].text, "Hello9999"); await f.bridge.close();
});

test("slow writer is single-flight with one coalesced follow-up", async () => {
  const f = fixture(); f.bridge.select("ses_A"); await f.settle();
  const gate = deferred(), file = f.files[0]; let concurrent = 0, peak = 0;
  const append = file.append.bind(file);
  file.append = async (data) => { peak = Math.max(peak, ++concurrent); await gate.promise; await append(data); concurrent--; };
  f.messages[0].content[0].text = "first"; f.event("session.message.content.updated", { messageID: "msg1" });
  await wait(10);
  for (let i = 0; i < 10000; i++) { f.messages[0].content[0].text = `latest${i}`; f.event("session.message.content.updated", { messageID: "msg1" }); }
  f.event("permission.asked", { id: "p", action: "shell" }); await f.bridge.reporter.idle();
  assert.equal(f.states().at(-1), "needs_input"); // State reporting does not wait for disk.
  gate.resolve(); await f.settle(); await wait(10); await f.settle();
  assert.equal(peak, 1); assert.equal(file.records()[0].content[0].text, "latest9999"); await f.bridge.close();
});

test("reporter bounds telemetry backlog while tuios is slow", async () => {
  const gate = deferred(), sent = [];
  const reporter = new Reporter(async (snapshot) => { await gate.promise; sent.push(snapshot); }, () => assert.fail());
  for (let i = 0; i < 10000; i++) reporter.submit({ generation: 1, state: i % 2 ? "done" : "working", terminal: i % 2 === 1, key: `${i}` });
  assert.ok(reporter.pending.length <= 3); assert.ok(reporter.maxPending <= 3);
  gate.resolve(); await reporter.idle(); assert.equal(sent.at(-1).key, "9999"); assert.ok(sent.length <= 4);
});

test("tab switch waits for the previous file write before reusing its path", async () => {
  const f = fixture(); f.bridge.select("ses_A"); await f.settle();
  const gate = deferred(), order = [], append = f.files[0].append.bind(f.files[0]);
  f.files[0].append = async (data) => { await gate.promise; await append(data); order.push("old finished"); };
  f.messages[0].content[0].text = "old update";
  f.event("session.message.content.updated", { messageID: "msg1" }); await wait(10);
  f.bridge.select("ses_B"); await tick(); assert.equal(f.files[1].writes.length, 0);
  const replace = f.files[1].replace.bind(f.files[1]);
  f.files[1].replace = async (data) => { order.push("new started"); await replace(data); };
  gate.resolve(); await f.settle();
  assert.deepEqual(order, ["old finished", "new started"]); assert.ok(f.files[0].closed);
  await f.bridge.close();
});

test("idle and unrelated metadata events cause no transcript writes", async () => {
  const f = fixture(); f.bridge.select("ses_A"); await f.settle(); const writes = f.files[0].writes.length;
  for (let i = 0; i < 100; i++) { f.event("session.viewed"); f.event("session.usage.updated"); }
  await wait(20); await f.settle(); assert.equal(f.files[0].writes.length, writes);
  assert.equal(f.bridge.current.timer, undefined); await f.bridge.close();
});

test("reasoning-only changes never reach the journal or spawn reporting commands", async () => {
  const f = fixture(); f.bridge.select("ses_A"); await f.settle();
  const calls = f.calls.length, writes = f.files[0].writes.length;
  f.messages[0].content.push({ type: "reasoning", text: "PRIVATE_THOUGHT" });
  f.event("session.reasoning.ended", { assistantMessageID: "msg1" });
  f.event("session.message.content.updated", { messageID: "msg1" });
  await wait(20); await f.settle();
  assert.equal(f.calls.length, calls); assert.equal(f.files[0].writes.length, writes);
  assert.ok(!f.files[0].data.includes("PRIVATE_THOUGHT")); await f.bridge.close();
});

test("missing tuios executable never breaks OpenCode", async () => {
  await commandRunner({ PATH: "/nonexistent" })(["set-agent-state", "idle"]);
});

test("installer previews, preserves settings, backs up and is idempotent", async () => {
  const dir = await temporary("tuios-v2-install-test-");
  try {
    const path = join(dir, "opencode/cli.json"); await mkdir(join(dir, "opencode"));
    await writeFile(path, JSON.stringify({ theme: { name: "keep-me" }, plugins: ["other-plugin"] }));
    const script = fileURLToPath(new URL("../install.py", import.meta.url));
    const opts = { env: { ...process.env, XDG_CONFIG_HOME: dir, TUIOS_HARNESS_DIR: join(dir, "harnesses") } };
    execFileSync("python3", [script], opts);
    assert.deepEqual(JSON.parse(await readFile(path)), { theme: { name: "keep-me" }, plugins: ["other-plugin"] });
    execFileSync("python3", [script, "--apply"], opts);
    const settings = JSON.parse(await readFile(path)); assert.equal(settings.theme.name, "keep-me");
    assert.ok(settings.plugins.includes("other-plugin")); assert.ok(settings.plugins.includes("-tuios-agent-state"));
    const backup = await readFile(`${path}.before-tuios-v2`, "utf8");
    execFileSync("python3", [script, "--apply"], opts); assert.deepEqual(JSON.parse(await readFile(path)), settings);
    assert.equal(await readFile(`${path}.before-tuios-v2`, "utf8"), backup);
    assert.ok((await readFile(join(dir, "harnesses/opencode.toml"), "utf8")).includes('reader = "gemini-chat"'));
    // The exact previous version upgrades; a custom manifest is never overwritten.
    const harness = join(dir, "harnesses/opencode.toml"), current = await readFile(harness, "utf8");
    await writeFile(harness, current.replace('reader = "gemini-chat"', 'reader = "amp-thread"').replace('glob = "*.jsonl"', 'glob = "*.json"'));
    execFileSync("python3", [script, "--apply"], opts); assert.equal(await readFile(harness, "utf8"), current);
    await writeFile(harness, current + "\n# my custom edit\n");
    const beforeRefusal = await readFile(path, "utf8");
    assert.throws(() => execFileSync("python3", [script, "--apply"], { ...opts, stdio: "pipe" }));
    assert.equal(await readFile(path, "utf8"), beforeRefusal);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
