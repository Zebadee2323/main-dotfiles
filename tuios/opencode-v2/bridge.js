import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";
import { Journal, MAX_TRACKED_MESSAGES } from "./transcript.js";
import { Reporter } from "./reporter.js";
import { errorSummary, isCancellation, writeDiagnostic } from "./diagnostics.js";

const CONTENT_EVENT = /^session\.(message\.|step\.|text\.|reasoning\.|tool\.|shell\.|inbox\.delivered)/;

export function transcriptPath(env, sessionID) {
  if (!/^ses_[a-zA-Z0-9]+$/.test(sessionID)) throw new Error("Invalid OpenCode session ID");
  const state = isAbsolute(env.XDG_STATE_HOME ?? "") ? env.XDG_STATE_HOME : join(homedir(), ".local/state");
  const pane = createHash("sha256").update(env.TUIOS_PANE_ID).digest("hex").slice(0, 16);
  return join(state, "tuios/opencode-v2", `${pane}.jsonl`);
}

// No shell, service discovery, credentials or permission replies. At most one
// reporting subprocess at a time; token/content events never spawn commands.
export function commandRunner(env = process.env) {
  return (args, input) => new Promise((resolve) => {
    const child = spawn("tuios", args, { env, stdio: ["pipe", "ignore", "ignore"] });
    const timer = setTimeout(() => { child.kill("SIGKILL"); resolve(); }, 2000);
    child.on("error", () => { clearTimeout(timer); resolve(); });
    child.on("close", () => { clearTimeout(timer); resolve(); });
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

export class Bridge {
  constructor(ctx, { env = process.env, run = commandRunner(env), journal = (path, id) => new Journal(path, id), interval = 750, diagnose = writeDiagnostic, retryDelays = [100, 250, 500, 1000] } = {}) {
    this.ctx = ctx;
    this.env = env;
    this.run = run;
    this.journal = journal;
    this.interval = interval;
    this.retryDelays = retryDelays;
    this.generation = 0;
    this.selected = undefined;
    this.current = undefined;
    this.closed = false;
    this.warned = new Set();
    this.diagnose = diagnose;
    this.retiring = new Set();
    this.reporter = new Reporter((snapshot) => this.send(snapshot), (error, snapshot) => {
      if (snapshot.generation === this.generation) this.warn(error, "report", this.current);
    });
  }

  warn(error, stage, current) {
    if (this.closed || current && !this.active(current) || isCancellation(error) || this.warned.has(stage)) return;
    this.warned.add(stage);
    const summary = errorSummary(error);
    const reason = summary.code ?? summary.status ?? summary.name;
    const message = stage.endsWith("sync")
      ? `${stage} unavailable (${reason}); using cached/live data.`
      : `Conversation mirror ${stage} failed (${reason}).`;
    this.ctx.ui.toast.show({ title: "TUIOS", message, variant: "warning" });
    if (current) void this.diagnose(current.path, stage, error).catch(() => {});
  }

  target() {
    return ["--window", this.env.TUIOS_PANE_ID,
      ...(this.env.TUIOS_SESSION ? ["--session", this.env.TUIOS_SESSION] : [])];
  }

  retire(current) {
    if (!current) return;
    clearTimeout(current.timer);
    for (const cancel of current.retryWaiters) cancel();
    const closing = Promise.resolve(current.task).then(() => current.journal.close()).catch((error) => this.warn(error, "close", current));
    this.retiring.add(closing);
    void closing.finally(() => this.retiring.delete(closing));
  }

  select(sessionID) {
    if (this.closed || sessionID === this.selected) return;
    this.selected = sessionID;
    this.retire(this.current);
    const generation = ++this.generation;
    this.current = undefined;
    // Release a previous conversation's identity before claiming a new one.
    this.reporter.submit({ generation, state: "none", key: `${generation}:none` });
    if (!sessionID) return;
    const path = transcriptPath(this.env, sessionID);
    const current = this.current = {
      sessionID, generation, state: "idle", revision: 0, permissions: new Map(), forms: new Map(),
      path, journal: this.journal(path, sessionID), dirty: new Set(), touched: new Set(),
      permissionTouched: new Set(), formTouched: new Set(), ready: false, scan: true, retryWaiters: new Set(),
      retired: Promise.all([...this.retiring]),
    };
    const sync = (stage, fn) => this.sync(current, stage, fn);
    // Optional permission/form hydration must not gate transcript startup.
    // Late failures from a previous tab (or expected cancellation) are ignored.
    void sync("permission-sync", () => this.ctx.data.session.permission.sync(sessionID)).then(() => {
      if (!this.active(current) || current.blockersReset) return;
      for (const p of this.ctx.data.session.permission.list(sessionID) ?? []) {
        if (!current.permissionTouched.has(p.id)) current.permissions.set(p.id, p.message || p.action);
      }
      this.report(current);
    }).catch((error) => this.warn(error, "permission-sync", current));
    void sync("form-sync", () => this.ctx.data.session.form.sync(sessionID)).then(() => {
      if (!this.active(current) || current.blockersReset) return;
      for (const f of this.ctx.data.session.form.list(sessionID) ?? []) {
        if (!current.formTouched.has(f.id)) current.forms.set(f.id, f.title);
      }
      this.report(current);
    }).catch((error) => this.warn(error, "form-sync", current));
    void Promise.all([
      sync("session-sync", () => this.ctx.data.session.sync(sessionID)),
      sync("history-sync", () => this.ctx.data.session.message.sync(sessionID)),
    ]).then(() => {
      if (!this.active(current)) return;
      current.ready = true;
      if (current.revision === 0) {
        current.state = this.ctx.data.session.status(sessionID) === "running" ? "working"
          : this.ctx.data.session.get(sessionID)?.outcome === "failed" ? "errored" : "idle";
      }
      if (!current.blockersReset) {
        for (const p of this.ctx.data.session.permission.list(sessionID) ?? []) {
          if (!current.permissionTouched.has(p.id)) current.permissions.set(p.id, p.message || p.action);
        }
        for (const f of this.ctx.data.session.form.list(sessionID) ?? []) {
          if (!current.formTouched.has(f.id)) current.forms.set(f.id, f.title);
        }
      }
      this.report(current);
      this.flush(current);
    }).catch((error) => this.warn(error, "history-sync", current));
  }

  active(current) { return !this.closed && this.current === current; }

  async sync(current, stage, fn) {
    // Keep cache reads outside the route-selection reactive effect.
    await Promise.resolve();
    // A new route can appear before its session is persisted. Retry only that
    // typed not-found error, with a finite budget; other failures still warn.
    for (let attempt = 0; this.active(current); attempt++) {
      try {
        return await fn();
      } catch (error) {
        if (!this.active(current)) return;
        const missing = error?._tag === "SessionNotFoundError" || error?.name === "SessionNotFoundError";
        if (!missing || attempt >= this.retryDelays.length) {
          this.warn(error, stage, current);
          return;
        }
        await new Promise((resolve) => {
          const finish = () => {
            clearTimeout(timer);
            current.retryWaiters.delete(finish);
            resolve();
          };
          const timer = setTimeout(finish, this.retryDelays[attempt]);
          current.retryWaiters.add(finish);
        });
      }
    }
  }

  event(event) {
    const current = this.current;
    if (!current || this.closed) return;
    const data = event.data ?? {};
    if ((data.sessionID ?? data.form?.sessionID) !== current.sessionID) return;
    const type = event.type;
    if (type.startsWith("session.reasoning.")) return;
    const execution = type.startsWith("session.execution.");
    const blocker = type.startsWith("permission.") || type.startsWith("form.");
    if (execution || type === "session.status") current.revision++;
    switch (type) {
      case "session.execution.started": current.state = "working"; current.message = undefined; break;
      case "session.execution.succeeded": current.state = "done"; break;
      case "session.execution.interrupted": current.state = "idle"; break;
      case "session.execution.failed": current.state = "errored"; current.message = data.error?.message; break;
      case "session.status":
        if (["busy", "running", "retry"].includes(data.status?.type)) current.state = "working";
        if (data.status?.type === "idle" && current.state === "working") current.state = "done";
        break;
      case "permission.asked":
        current.permissionTouched.add(data.id); current.permissions.set(data.id, data.message || data.action); break;
      case "permission.replied":
        current.permissionTouched.add(data.requestID); current.permissions.delete(data.requestID); break;
      case "form.created":
        current.formTouched.add(data.form.id); current.forms.set(data.form.id, data.form.title); break;
      case "form.replied":
      case "form.cancelled":
        current.formTouched.add(data.id); current.forms.delete(data.id); break;
      case "session.deleted": this.select(undefined); return;
    }
    if (execution) {
      current.blockersReset = true;
      current.permissions.clear(); current.forms.clear();
      current.scan = true; // Attach/turn-boundary discovery, not per-token scans.
    }
    const messageID = data.messageID ?? data.assistantMessageID ?? data.inboxID ?? data.shellMessageID;
    const content = CONTENT_EVENT.test(type);
    if (content) {
      if (messageID) {
        current.dirty.add(messageID);
        current.touched.add(messageID);
        // Bound dirty/touched IDs even for a malformed or enormous event stream.
        if (current.dirty.size > MAX_TRACKED_MESSAGES) { current.dirty.clear(); current.scan = true; }
        if (current.touched.size > MAX_TRACKED_MESSAGES) current.touched.delete(current.touched.values().next().value);
      } else current.scan = true;
    }
    if (execution || blocker || type === "session.status" || type === "session.usage.updated" || type === "session.model.selected") this.report(current);
    if (execution || blocker) this.flush(current);
    else if (content) this.schedule(current);
  }

  schedule(current) {
    if (!current.ready || current.timer || current.task || !this.active(current)) return;
    current.timer = setTimeout(() => { current.timer = undefined; this.flush(current); }, this.interval);
  }

  flush(current) {
    if (!this.active(current) || !current.ready) return;
    clearTimeout(current.timer); current.timer = undefined;
    // Single-flight writer: events arriving during I/O only mark dirty IDs.
    // No queued callbacks, copied history, or periodic wakeups during silence.
    if (current.task) { current.again = true; return; }
    current.task = Promise.resolve().then(async () => {
      await current.retired; // One path per pane: never race an old session's final write.
      if (!this.active(current)) return;
      const dirty = current.dirty;
      current.dirty = new Set();
      if (current.scan) {
        current.scan = false;
        const touched = current.touched;
        current.touched = new Set();
        await current.journal.reconcileAsync(this.ctx.data.session.message.list(current.sessionID), touched, () => this.active(current));
        if (!this.active(current)) return;
      } else {
        for (const id of dirty) {
          const message = this.ctx.data.session.message.get(current.sessionID, id);
          if (message) current.journal.update(message);
          else current.scan = true; // One deferred discovery if cache hydration lagged the event.
        }
      }
      await current.journal.flush();
      if (this.active(current) && current.journal.initialized && !current.pathReported) {
        current.pathReported = true;
        this.report(current); // Join only after the initial file exists.
      }
    }).catch((error) => this.warn(error, "write", current)).finally(() => {
      current.task = undefined;
      if (current.again) { current.again = false; this.flush(current); }
      else if (current.dirty.size || current.scan) this.schedule(current);
    });
  }

  report(current) {
    if (!this.active(current)) return;
    const permission = current.permissions.values().next().value;
    const form = current.forms.values().next().value;
    const state = current.permissions.size || current.forms.size ? "needs_input" : current.state;
    const kind = current.permissions.size ? "approval" : current.forms.size ? "question" : undefined;
    const message = kind ? permission ?? form : state === "errored" ? current.message : undefined;
    const session = this.ctx.data.session.get(current.sessionID);
    const usage = JSON.stringify({ session_id: current.sessionID, modelID: session?.model?.id, cost: session?.cost });
    const terminal = ["done", "errored", "idle"].includes(state);
    const path = current.pathReported ? current.path : undefined;
    const stateKey = JSON.stringify([current.generation, state, kind, message, path]);
    const usageKey = `${current.generation}|${usage}|${terminal}`;
    this.reporter.submit({ generation: current.generation, sessionID: current.sessionID, state, kind, message,
      path, usage, terminal, stateKey, usageKey, key: `${stateKey}|${usageKey}` });
  }

  async send(snapshot) {
    const { state, sessionID, kind, message, path } = snapshot;
    if (state === "none") {
      await this.run(["set-agent-state", "none", "--harness", "opencode", ...this.target()]);
      this.sentState = undefined; this.sentUsage = undefined;
      return;
    }
    if (this.sentState !== snapshot.stateKey) {
      await this.run(["set-agent-state", state, "--harness", "opencode", ...this.target(), "--agent-session-id", sessionID,
        ...(path ? ["--transcript-path", path] : []), ...(kind ? ["--kind", kind] : []),
        ...(message ? ["--message", String(message).slice(0, 240)] : [])]);
      this.sentState = snapshot.stateKey;
    }
    if (snapshot.generation !== this.generation) return;
    if (this.sentUsage !== snapshot.usageKey) {
      await this.run(["agent-statusline", "opencode", ...this.target(), ...(snapshot.terminal ? ["--turn-end"] : [])], snapshot.usage);
      this.sentUsage = snapshot.usageKey;
    }
  }

  async idle() {
    await this.current?.task;
    await this.reporter.idle();
  }

  async close() {
    this.closed = true;
    this.retire(this.current);
    this.current = undefined;
    this.reporter.submit({ generation: ++this.generation, state: "none", key: `${this.generation}:none` });
    await Promise.all([...this.retiring, this.reporter.idle()]);
  }
}
