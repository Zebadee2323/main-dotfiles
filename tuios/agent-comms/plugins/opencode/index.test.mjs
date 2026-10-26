import assert from "node:assert/strict"
import { test } from "node:test"
import { createPlugin } from "./index.js"

async function harness(run) {
  let hook
  const plugin = createPlugin(run)
  await plugin.setup({ options: {}, session: { hook: async (name, fn) => {
    assert.equal(name, "context")
    hook = fn
  } } })
  return hook
}

test("appends context without replacing existing instructions or duplicating itself", async () => {
  const calls = []
  const hook = await harness(async (python, args, options) => {
    calls.push({ python, args, options })
    return { stdout: "Look up agent-comms context once; send with --id RETURNED_ID.\nAlways close every turn with agent-comms send." }
  })
  const original = { type: "text", text: "Existing system instructions" }
  const event = { sessionID: "ses_test", system: [original] }
  await hook(event)
  await hook(event)
  assert.equal(event.system.length, 2)
  assert.equal(event.system[0], original)
  assert.match(event.system[1].text, /send with --id RETURNED_ID/)
  assert.equal(calls.length, 1, "cache instructions only, not a pane identity or prefix")
  assert.ok(calls[0].args.includes("instructions"))
  assert.ok(!calls[0].args.includes("--session-id"))
  assert.ok(!calls[0].args.includes("--harness"))
  assert.equal(calls[0].options.timeout, 15000)
})

test("coalesces concurrent initialization, handles new sessions and post-compaction requests", async () => {
  let calls = 0
  const hook = await harness(async (_, args) => {
    calls++
    await new Promise(resolve => setImmediate(resolve))
    return { stdout: "Always call agent-comms context before sending." }
  })
  const one = { sessionID: "ses_one", system: [] }
  const two = { sessionID: "ses_one", system: [] }
  await Promise.all([hook(one), hook(two)])
  assert.equal(calls, 1)
  assert.deepEqual(one.system, two.system)
  const resumed = { sessionID: "ses_one", system: [] }
  await hook(resumed)
  assert.deepEqual(resumed.system, one.system)
  const child = { sessionID: "ses_child", system: [] }
  await hook(child)
  assert.deepEqual(child.system, one.system, "all sessions receive the same owner-free instructions")
})

test("setup never draws; missing session IDs and unavailable helpers do not block work", async () => {
  let calls = 0
  const hook = await harness(async () => { calls++; throw new Error("private exec details") })
  assert.equal(calls, 0)
  const event = { sessionID: "ses_offline", system: [] }
  const original = console.error
  const warnings = []
  console.error = message => warnings.push(message)
  try {
    await hook({ system: [] })
    await hook(event)
    await hook(event)
  } finally { console.error = original }
  assert.equal(calls, 1)
  assert.equal(warnings.length, 1)
  assert.doesNotMatch(warnings[0], /private exec details/)
  assert.deepEqual(event.system, [])
})
