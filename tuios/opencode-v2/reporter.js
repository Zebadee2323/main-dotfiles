// Reporting is independent of filesystem work. Keep at most one running
// command and three pending snapshots (identity reset, latest turn end, latest
// state). Under overload discard superseded telemetry, never queue token work.
export class Reporter {
  constructor(send, onError) {
    this.send = send;
    this.onError = onError;
    this.pending = [];
    this.running = undefined;
    this.generation = 0;
    this.last = undefined;
    this.maxPending = 0;
  }

  submit(snapshot) {
    if (snapshot.generation !== this.generation) {
      this.generation = snapshot.generation;
      this.pending.length = 0;
    }
    const last = this.pending.at(-1);
    if (last?.key === snapshot.key) return;
    this.pending.push(snapshot);
    if (this.pending.length > 3) {
      const reset = this.pending.find((p) => p.state === "none");
      const end = this.pending.findLast((p) => p.terminal);
      this.pending = [...new Set([reset, end, snapshot].filter(Boolean))];
    }
    this.maxPending = Math.max(this.maxPending, this.pending.length);
    this.start();
  }

  start() {
    if (this.running || !this.pending.length) return;
    this.running = this.drain().finally(() => {
      this.running = undefined;
      this.start();
    });
  }

  async drain() {
    while (this.pending.length) {
      const snapshot = this.pending.shift();
      if (this.last === snapshot.key) continue;
      try { await this.send(snapshot); }
      catch (error) { this.onError(error, snapshot); }
      this.last = snapshot.key;
    }
  }

  async idle() {
    // A submit can occur while a previous drain's final promise settles.
    while (this.running || this.pending.length) {
      this.start();
      await this.running;
    }
  }
}
