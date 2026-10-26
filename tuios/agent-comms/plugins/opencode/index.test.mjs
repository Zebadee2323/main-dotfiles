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
    return { stdout: "Your assigned identity is echo.\nAlways close every turn with agent-comms send." }
  })
  const original = { type: "text", text: "Existing system instructions" }
  const event = { sessionID: "ses_test", system: [original] }
  await hook(event)
  await hook(event)
  assert.equal(event.system.length, 2)
  assert.equal(event.system[0], original)
  assert.match(event.system[1].text, /echo/)
  assert.equal(calls.length, 1, "reuse the session assignment across tool continuations")
  assert.ok(calls[0].args.includes("ses_test"))
  assert.ok(calls[0].args.includes("opencode"))
  assert.equal(calls[0].options.timeout, 15000)
})

test("coalesces concurrent initialization, handles new sessions and post-compaction requests", async () => {
  let calls = 0
  const hook = await harness(async (_, args) => {
    calls++
    await new Promise(resolve => setImmediate(resolve))
    return { stdout: `Identity for ${args[args.indexOf("--session-id") + 1]}` }
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
  assert.notDeepEqual(child.system, one.system)
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
