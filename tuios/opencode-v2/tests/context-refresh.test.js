import assert from "node:assert/strict";
import { test } from "node:test";
import { ContextRefresh } from "../context-refresh.js";

const tick = () => new Promise((resolve) => setImmediate(resolve));
async function settle(refresh) {
  await tick();
  await refresh.task;
  await tick();
  await refresh.task;
}

test("refreshes after selection and title changes, not unchanged updates", async () => {
  let calls = 0;
  const refresh = new ContextRefresh(async () => { calls++; return true; });
  refresh.select("ses_A", "First task");
  assert.equal(calls, 0); // Native title effects get to run first.
  await settle(refresh);
  assert.equal(calls, 1);
  refresh.select("ses_A", "First task");
  await settle(refresh);
  assert.equal(calls, 1);
  refresh.select("ses_A", "Generated title");
  await settle(refresh);
  refresh.select("ses_B", "Generated title");
  await settle(refresh);
  assert.equal(calls, 3);
  await refresh.close();
});

test("coalesces rapid switches and waits for session hydration", async () => {
  let calls = 0;
  const refresh = new ContextRefresh(async () => { calls++; return true; });
  refresh.select("ses_A", undefined);
  await settle(refresh);
  assert.equal(calls, 0);
  refresh.select("ses_A", "A");
  refresh.select("ses_B", "B");
  await settle(refresh);
  assert.equal(calls, 1);
  refresh.select("ses_C", "C");
  refresh.select(undefined, undefined);
  await settle(refresh);
  assert.equal(calls, 1);
  await refresh.close();
});

test("serializes lookups and retains only the latest pending switch", async () => {
  let release, calls = 0, active = 0, maxActive = 0;
  const gate = new Promise((resolve) => { release = resolve; });
  const refresh = new ContextRefresh(async () => {
    calls++; maxActive = Math.max(maxActive, ++active);
    if (calls === 1) await gate;
    active--;
    return true;
  });
  refresh.select("ses_A", "A");
  await tick();
  refresh.select("ses_B", "B");
  refresh.select("ses_C", "C");
  release();
  await settle(refresh);
  assert.equal(calls, 2);
  assert.equal(maxActive, 1);
  await refresh.close();
});

test("failure is advisory once, not retried, and does not block later switches", async () => {
  let calls = 0, warnings = 0;
  const refresh = new ContextRefresh(async () => { calls++; throw new Error("offline"); }, () => warnings++);
  refresh.select("ses_A", "A");
  await settle(refresh);
  assert.equal(calls, 1);
  refresh.select("ses_B", "B");
  await settle(refresh);
  assert.equal(calls, 2);
  assert.equal(warnings, 1);
  await refresh.close();
});

test("unload cancels pending work and aborts an active lookup", async () => {
  let calls = 0;
  const pending = new ContextRefresh(async () => { calls++; return true; });
  pending.select("ses_A", "A");
  await pending.close();
  await tick();
  assert.equal(calls, 0);
  const running = new ContextRefresh((signal) => new Promise((resolve) => {
    calls++;
    signal.addEventListener("abort", () => resolve(false), { once: true });
  }));
  running.select("ses_A", "A");
  await tick();
  await running.close();
  assert.equal(calls, 1);
});
