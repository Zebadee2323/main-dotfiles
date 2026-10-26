import { readFile } from "node:fs/promises"
import { Plugin } from "@opencode/plugin"

// Loaded only by the Sidekick launch-local configuration, never discovered as
// a project/global plugin. Instructions belong to requests, not saved sessions.
export default Plugin.define({
  id: "naia.sidekick",
  async setup(ctx) {
    const instructions = await readFile(new URL("../../bin/agent-instructions.md", import.meta.url), "utf8")
    if (!instructions.trim()) throw new Error("Naia instructions must not be empty")

    await ctx.session.hook("context", (event) => {
      // An older launcher may already have stored the same session entry.
      if (!event.system.some((part) => part.type === "text" && part.text.includes(instructions))) {
        event.system.push({ type: "text", text: instructions })
      }
    })
  },
})
