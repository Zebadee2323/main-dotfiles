// Run with: node --experimental-vm-modules --test tests/naia/test_opencode_plugin.mjs
import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import { test } from "node:test"
import { SourceTextModule, SyntheticModule } from "node:vm"

const pluginURL = new URL("../../opencode/naia/index.js", import.meta.url)
const instructionsURL = new URL("../../bin/agent-instructions.md", import.meta.url)
const instructions = await readFile(instructionsURL, "utf8")

async function loadPlugin(read = readFile) {
  const module = new SourceTextModule(await readFile(pluginURL, "utf8"), {
    initializeImportMeta(meta) { meta.url = pluginURL.href },
  })
  await module.link((name) => {
    if (name === "@opencode/plugin") {
      return new SyntheticModule(["Plugin"], function () {
        this.setExport("Plugin", { define: (definition) => definition })
      })
    }
    assert.equal(name, "node:fs/promises")
    return new SyntheticModule(["readFile"], function () { this.setExport("readFile", read) })
  })
  await module.evaluate()
  return module.namespace.default
}

test("every session and tool continuation receives instructions without replacing the system prompt", async () => {
  const plugin = await loadPlugin()
  const hooks = new Map()
  await plugin.setup({ session: { hook: async (name, fn) => hooks.set(name, fn) } })
  assert.equal(plugin.id, "naia.sidekick")
  assert.deepEqual([...hooks.keys()], ["context"])
  for (const sessionID of ["ses_initial", "ses_new_inside_tui", "ses_resumed", "ses_child", "ses_after_compaction", "ses_initial"]) {
    const event = { sessionID, system: [{ type: "text", text: "Original OpenCode system prompt" }] }
    hooks.get("context")(event)
    assert.equal(event.system.length, 2)
    assert.equal(event.system[0].text, "Original OpenCode system prompt")
    assert.equal(event.system[1].text, instructions)
    hooks.get("context")(event)
    assert.equal(event.system.length, 2, "do not duplicate existing instructions")
  }
})

test("identical legacy session instructions are not duplicated", async () => {
  const plugin = await loadPlugin()
  let contextHook
  await plugin.setup({ session: { hook: async (_, fn) => { contextHook = fn } } })
  const event = { system: [{ type: "text", text: `<context key="naia">\n${instructions}\n</context>` }] }
  contextHook(event)
  assert.equal(event.system.length, 1)
})

test("empty or unreadable instructions fail plugin setup before any hook is installed", async () => {
  for (const read of [async () => "  ", async () => { throw new Error("file unavailable") }]) {
    const plugin = await loadPlugin(read)
    let registered = false
    await assert.rejects(plugin.setup({ session: { hook: async () => { registered = true } } }))
    assert.equal(registered, false)
  }
})
