// Legacy rich mirror vs current compact mirror, each in a fresh process.
// Default: real projection/encoding, counting writes without disk I/O.
// --disk: actual private temporary files, 20 updates instead of 100.
import { spawnSync } from "node:child_process";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import { Journal, JournalFile } from "./transcript.js";

const args = process.argv.slice(2);
const disk = args.includes("--disk");
const worker = args.indexOf("--worker");

// The previous implementation's text-only projection, including its per-message
// sizing pass and whole-document encode. Current policy intentionally retains
// less content; this measures the combined benefit, not an equal-content test.
function baseline(messages) {
  const output = messages.slice(-500).map((m) => ({ messageId: m.id,
    meta: { sentAt: m.time.created }, role: "assistant",
    content: m.content.map((c) => ({ type: "text", text: c.text.slice(0, 32768) })) }));
  let bytes = 0, first = output.length;
  while (first > 0) {
    const size = Buffer.byteLength(JSON.stringify(output[first - 1])) + 1;
    if (bytes + size > 16 * 1024 * 1024) break;
    bytes += size; first--;
  }
  return JSON.stringify({ id: "ses_Bench", messages: output.slice(first) });
}

if (worker >= 0) {
  const strategy = args[worker + 1], count = Number(args[worker + 2]);
  const iterations = disk ? 20 : 100;
  const history = Array.from({ length: count }, (_, i) => ({ id: `msg${i}`, type: "assistant", time: { created: i },
    content: [{ type: "text", text: i === count - 1 ? "current" : "x".repeat(32768) }] }));
  const last = history.at(-1);
  const root = join(tmpdir(), "opencode");
  if (disk) await mkdir(root, { recursive: true });
  const directory = disk ? await mkdtemp(join(root, "tuios-v2-bench-")) : undefined;
  let written = 0, writes = 0;
  const counter = { replace: async (s) => { written += Buffer.byteLength(s); writes++; },
    append: async (s) => { written += Buffer.byteLength(s); writes++; }, close: async () => {} };
  const file = disk ? new JournalFile(join(directory, "bench.jsonl")) : counter;
  const journal = new Journal("unused", "ses_Bench", { file });
  try {
    if (strategy === "baseline") {
      const data = baseline(history); await file.replace(data);
      if (disk) { written += Buffer.byteLength(data); writes++; }
    } else { journal.reconcile(history); await journal.flush(); }
    const initialBytes = strategy === "baseline" ? written : journal.stats.bytes;
    written = 0; writes = 0;
    const initialWrites = journal.stats.replacements + journal.stats.appends;
    const initialEncodes = journal.stats.encodedRecords;
    global.gc?.();
    const cpu = process.cpuUsage(), start = performance.now();
    for (let i = 0; i < iterations; i++) {
      last.content[0].text += `chunk${i}:` + "y".repeat(64);
      if (strategy === "baseline") {
        const data = baseline(history); await file.replace(data);
        if (disk) { written += Buffer.byteLength(data); writes++; }
      } else { journal.update(last); await journal.flush(); }
    }
    const elapsedMs = performance.now() - start, used = process.cpuUsage(cpu);
    console.log(JSON.stringify({ strategy, messages: count, iterations, disk, initialBytes,
      retainedMessages: strategy === "baseline" ? Math.min(count, 500) : journal.messages.size,
      steadyBytes: strategy === "baseline" ? written : journal.stats.bytes - initialBytes,
      writes: strategy === "baseline" ? writes : journal.stats.replacements + journal.stats.appends - initialWrites,
      encodedRecords: strategy === "baseline" ? count * iterations : journal.stats.encodedRecords - initialEncodes,
      cpuMs: (used.user + used.system) / 1000, elapsedMs, peakRSSMiB: process.resourceUsage().maxRSS / 1024 }));
  } finally { await file.close(); if (directory) await rm(directory, { recursive: true, force: true }); }
} else {
  console.log(`Node ${process.version}; ${disk ? "actual temporary-file writes" : "logical writes, no disk I/O"}; initialization excluded from steady-state timing. Legacy 500-message/32-Ki-character policy vs compact 40-message/4-KiB policy (different retained content).`);
  const rows = [];
  for (const count of [33, 500]) for (const strategy of ["baseline", "optimized"]) {
    const child = spawnSync(process.execPath, ["--expose-gc", fileURLToPath(import.meta.url), "--worker", strategy, String(count), ...(disk ? ["--disk"] : [])],
      { encoding: "utf8", timeout: 120000 });
    if (child.status !== 0) throw new Error(child.stderr || child.error?.message || "Benchmark failed");
    rows.push(JSON.parse(child.stdout));
  }
  console.table(rows.map((r) => ({ strategy: r.strategy, sourceMessages: r.messages, retained: r.retainedMessages, updates: r.iterations,
    initialKiB: +(r.initialBytes / 1024).toFixed(2), writtenKiB: +(r.steadyBytes / 1024).toFixed(2),
    cpuMs: +r.cpuMs.toFixed(2), elapsedMs: +r.elapsedMs.toFixed(2), peakRSSMiB: +r.peakRSSMiB.toFixed(1) })));
  console.log(JSON.stringify(rows, null, 2));
}
