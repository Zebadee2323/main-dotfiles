import { spawn } from "node:child_process";

// This runs in the terminal client's pane environment, never the shared server.
// The JSON identity is deliberately discarded: this hook only refreshes the name.
export function contextRunner(env = process.env) {
  return (signal) => new Promise((resolve) => {
    const child = spawn("agent-comms", ["context"], { env, signal, stdio: "ignore" });
    const timer = setTimeout(() => { child.kill("SIGKILL"); resolve(false); }, 10000);
    child.on("error", () => { clearTimeout(timer); resolve(false); });
    child.on("close", (code) => { clearTimeout(timer); resolve(code === 0); });
  });
}

// One in-flight lookup and at most one pending refresh. No polling or token work.
export class ContextRefresh {
  constructor(run = contextRunner(), warn = () => {}) {
    this.run = run;
    this.warn = warn;
    this.controller = new AbortController();
  }

  select(sessionID, title) {
    if (this.closed) return;
    const key = sessionID && typeof title === "string" ? JSON.stringify([sessionID, title]) : undefined;
    if (key === this.key) return;
    this.key = key;
    this.pending = key;
    clearImmediate(this.timer);
    this.timer = undefined;
    if (!key || this.task) return;
    // Let OpenCode's native reactive title update run first. We never overwrite
    // its terminal-title policy or write a competing tuios custom pane name.
    this.timer = setImmediate(() => { this.timer = undefined; this.start(); });
  }

  start() {
    if (this.closed || this.task || !this.pending) return;
    const key = this.pending;
    this.pending = undefined;
    this.task = Promise.resolve().then(() => this.run(this.controller.signal))
      .catch(() => false).then((ok) => {
        if (!ok && !this.closed && key === this.key && !this.warned) {
          this.warned = true;
          this.warn();
        }
      }).finally(() => {
        this.task = undefined;
        if (!this.closed && this.pending) {
          this.timer = setImmediate(() => { this.timer = undefined; this.start(); });
        }
      });
  }

  async close() {
    this.closed = true;
    clearImmediate(this.timer);
    this.pending = undefined;
    this.controller.abort();
    await this.task;
  }
}
