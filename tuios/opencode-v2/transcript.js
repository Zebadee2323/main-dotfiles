import { mkdir, open, rename, unlink } from "node:fs/promises";
import { constants } from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname } from "node:path";

export const MAX_MESSAGES = 40;
export const MAX_TOOL_MESSAGES = 8;
export const MAX_TRACKED_MESSAGES = MAX_MESSAGES + MAX_TOOL_MESSAGES;
export const MAX_LIVE_BYTES = 256 * 1024;
export const COMPACT_SLACK = 64 * 1024;
export const MAX_TEXT_BYTES = 4096;
const MAX_TOOLS = 8;
const MAX_PARTS = 256;

// Bound bytes, not characters, without scanning/copying a huge source string.
// A visible marker makes the loss explicit; never split a UTF-8 code point.
export function text(value, limit = MAX_TEXT_BYTES) {
  if (typeof value !== "string" || limit <= 0) return "";
  const head = value.slice(0, limit);
  const bytes = Buffer.from(head);
  if (head.length === value.length && bytes.length <= limit) return head;
  const marker = "… [truncated]";
  if (limit < Buffer.byteLength(marker)) return "";
  let end = Math.min(bytes.length, limit - Buffer.byteLength(marker));
  while (end > 0 && (bytes[end] & 0xc0) === 0x80) end--;
  return bytes.subarray(0, end).toString("utf8") + marker;
}

function messageKind(message) {
  if (message.type === "user") return "conversation";
  if (message.type === "shell") return "tool";
  if (message.type !== "assistant") return undefined;
  if (message.error?.message) return "conversation";
  let tool = false;
  for (const part of (message.content ?? []).slice(0, MAX_PARTS)) {
    if (part.type === "text" && part.text) return "conversation";
    if (part.type === "tool") tool = true;
  }
  return tool ? "tool" : undefined;
}

// Gemini's JSONL reader supports keyed replacement, stable entry IDs, tool
// status and cursor resets. Reuse that encoding, NOT its harness identity.
// A separate key per OpenCode content part avoids repeating earlier tool/text
// blocks whenever the last part streams. Record sizes and part count are capped.
export function recordsFor(message) {
  const records = [];
  const timestamp = new Date(message.time?.created ?? 0).toISOString();
  const add = (suffix, type, fields) => records.push({ id: `${message.id}:${suffix}`, timestamp, type, ...fields });
  if (message.type === "user") {
    const attachments = [];
    for (const file of (message.files ?? []).slice(0, 8)) {
      if (file.mime?.startsWith("image/")) attachments.push({ fileData: {} });
      else attachments.push({ text: "[attachment]" });
    }
    const reserved = attachments.reduce((n, part) => n + Buffer.byteLength(part.text ?? ""), 0);
    const content = [{ text: text(message.text, MAX_TEXT_BYTES - reserved) }, ...attachments];
    add("user", "user", { content });
  } else if (message.type === "assistant") {
    let remaining = MAX_TEXT_BYTES;
    let lastText, omittedText = false;
    const parts = (message.content ?? []).slice(0, MAX_PARTS);
    const tools = parts.filter((part) => part.type === "tool").slice(-MAX_TOOLS);
    for (const [index, part] of parts.entries()) {
      if (part.type === "text") {
        if (remaining <= 0) { if (part.text) omittedText = true; continue; }
        const value = text(part.text, remaining);
        remaining -= Buffer.byteLength(value);
        if (value) { add(index, "gemini", { content: [{ text: value }] }); lastText = records.at(-1); }
        else if (part.text) omittedText = true;
      } else if (part.type === "tool") {
        if (!tools.includes(part)) continue;
        const state = part.state ?? {};
        const failed = state.status === "error";
        const complete = failed || state.status === "completed";
        // Names + status only. Do not even read raw input/output/patch fields.
        // A successful result needs only an empty marker for TUIOS's status badge.
        add(index, "gemini", { toolCalls: [{
          id: `${message.id}:${index}`, name: text(part.name, 96),
          status: failed ? "error" : complete ? "success" : "executing",
          ...(complete ? { result: failed ? text(state.error?.message, 256).split(/\r?\n/, 1)[0] : "" } : {}),
        }] });
      }
    }
    if (omittedText && lastText && !lastText.content[0].text.endsWith("… [truncated]")) {
      const value = lastText.content[0].text;
      lastText.content[0].text = text(value + "[additional text omitted]", Buffer.byteLength(value));
    }
    if (message.error?.message) add("error", "error", { content: text(message.error.message, 256).split(/\r?\n/, 1)[0] });
  } else if (message.type === "shell") {
    const failed = message.status && message.status !== "running" && (message.status !== "exited" || message.exit !== 0);
    add("shell", "user", { content: failed ? "[Shell command failed]" : message.status === "running" ? "[Shell command running]" : "[Shell command]" });
  }
  return records;
}

function encode(record) {
  return JSON.stringify(record) + "\n";
}

// One persistent append handle, only while this session is displayed. The file
// is private, and a rare compaction atomically replaces it with a new cursor ID.
export class JournalFile {
  constructor(path) { this.path = path; }

  async replace(data) {
    await mkdir(dirname(this.path), { recursive: true, mode: 0o700 });
    const temporary = `${this.path}.${randomUUID()}.tmp`;
    try {
      const file = await open(temporary, "wx", 0o600);
      try { await file.writeFile(data); }
      finally { await file.close(); }
      await this.close();
      await rename(temporary, this.path);
      this.handle = await open(this.path, constants.O_WRONLY | constants.O_APPEND | constants.O_NOFOLLOW);
    } finally {
      await unlink(temporary).catch((e) => { if (e.code !== "ENOENT") throw e; });
    }
  }

  async append(data) { await this.handle.writeFile(data); }
  async close() {
    const handle = this.handle;
    this.handle = undefined;
    await handle?.close();
  }
}

export class Journal {
  constructor(path, sessionID, { file = new JournalFile(path), maxBytes = MAX_LIVE_BYTES, compactSlack = COMPACT_SLACK } = {}) {
    this.file = file;
    this.sessionID = sessionID;
    this.maxBytes = maxBytes;
    this.compactSlack = compactSlack;
    this.messages = new Map();
    this.conversations = new Set();
    this.toolMessages = new Set();
    this.known = new Set(); // Includes messages omitted by the byte retention cap.
    this.pending = new Map();
    this.persisted = new Map();
    this.liveBytes = 0;
    this.diskBytes = 0;
    this.compact = true;
    this.initialized = false;
    this.stats = { bytes: 0, appends: 0, replacements: 0, encodedRecords: 0 };
  }

  has(id) { return this.known.has(id); }

  update(message) {
    if (!message?.id) return;
    const kind = messageKind(message);
    if (!kind) { this.remove(message.id); return; }
    this.known.add(message.id);
    while (this.known.size > MAX_TRACKED_MESSAGES) this.known.delete(this.known.values().next().value);
    const group = kind === "conversation" ? this.conversations : this.toolMessages;
    (kind === "conversation" ? this.toolMessages : this.conversations).delete(message.id);
    group.add(message.id);
    const old = this.messages.get(message.id);
    const next = new Map();
    for (const record of recordsFor(message)) {
      const line = encode(record);
      this.stats.encodedRecords++;
      const entry = { line, bytes: Buffer.byteLength(line) };
      next.set(record.id, entry);
      if (this.persisted.get(record.id) !== line) this.pending.set(record.id, entry);
      else this.pending.delete(record.id); // A changed-then-reverted part is a no-op.
    }
    if (old) {
      for (const [id, entry] of old) {
        this.liveBytes -= entry.bytes;
        if (!next.has(id)) { this.pending.delete(id); this.persisted.delete(id); this.compact = true; }
      }
    }
    this.messages.set(message.id, next);
    for (const entry of next.values()) this.liveBytes += entry.bytes;
    while (this.conversations.size > MAX_MESSAGES) this.remove(this.conversations.values().next().value);
    while (this.toolMessages.size > MAX_TOOL_MESSAGES) this.remove(this.toolMessages.values().next().value);
    while (this.liveBytes > this.maxBytes) this.remove(this.messages.keys().next().value);
  }

  remove(id) {
    const entries = this.messages.get(id);
    if (!entries) return;
    for (const [key, entry] of entries) { this.liveBytes -= entry.bytes; this.pending.delete(key); this.persisted.delete(key); }
    this.messages.delete(id);
    this.conversations.delete(id);
    this.toolMessages.delete(id);
    this.compact = true;
  }

  retain(messages) {
    // Tool-heavy turns must not push the user's prompt out of the conversation.
    const newest = [];
    let conversations = 0, tools = 0;
    for (let i = messages.length - 1; i >= 0; i--) {
      const message = messages[i], kind = messageKind(message);
      if (kind === "conversation" && conversations < MAX_MESSAGES) { newest.push(message); conversations++; }
      if (kind === "tool" && tools < MAX_TOOL_MESSAGES) { newest.push(message); tools++; }
      if (conversations === MAX_MESSAGES) break; // Older tools are outside the conversation window.
    }
    newest.reverse();
    const keep = new Set(newest.map((m) => m.id));
    for (const id of this.known) if (!keep.has(id)) this.known.delete(id);
    for (const id of this.messages.keys()) {
      if (keep.has(id)) continue;
      this.remove(id);
    }
    return newest;
  }

  // Reconcile after attach/turn boundaries. Already persisted history is not
  // re-encoded; only unseen or explicitly touched message IDs are projected.
  reconcile(messages, touched = new Set()) {
    for (const message of this.retain(messages)) if (!this.has(message.id) || touched.has(message.id)) this.update(message);
  }

  // Yield between batches so loading a large source history
  // conversation does not monopolize the terminal's JavaScript event loop.
  async reconcileAsync(messages, touched, active) {
    let processed = 0;
    for (const message of this.retain(messages)) {
      if (!active()) return;
      if (this.has(message.id) && !touched.has(message.id)) continue;
      this.update(message);
      if (++processed % 32 === 0) await new Promise((resolve) => setImmediate(resolve));
    }
  }

  async flush() {
    if (!this.compact && !this.pending.size) return false;
    // Bound obsolete revisions on disk. Compaction cost is amortized over at
    // least 64 KiB of stale appends, not paid for every token or state change.
    let pendingBytes = 0;
    for (const entry of this.pending.values()) pendingBytes += entry.bytes;
    if (this.diskBytes + pendingBytes > this.liveBytes * 2 + this.compactSlack) this.compact = true;
    if (this.compact) {
      const header = JSON.stringify({ sessionId: this.sessionID, projection: "opencode-v2", revision: randomUUID() }) + "\n";
      const chunks = [header];
      for (const entries of this.messages.values()) for (const entry of entries.values()) chunks.push(entry.line);
      const data = chunks.join("");
      await this.file.replace(data);
      this.diskBytes = Buffer.byteLength(data);
      this.stats.bytes += this.diskBytes;
      this.stats.replacements++;
      this.initialized = true;
      this.persisted.clear();
      for (const entries of this.messages.values()) for (const [id, entry] of entries) this.persisted.set(id, entry.line);
    } else {
      const data = [...this.pending.values()].map((entry) => entry.line).join("");
      try { await this.file.append(data); }
      catch (error) { this.compact = true; throw error; } // Repair a partial append before any retry.
      const bytes = Buffer.byteLength(data);
      this.diskBytes += bytes;
      this.stats.bytes += bytes;
      this.stats.appends++;
      for (const [id, entry] of this.pending) this.persisted.set(id, entry.line);
    }
    this.pending.clear();
    this.compact = false;
    return true;
  }

  async close() { await this.file.close(); }
}
