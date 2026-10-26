import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { fileURLToPath } from "node:url"

const execute = promisify(execFile)
const helper = fileURLToPath(new URL("../../agent_comms.py", import.meta.url))

// V2's native object entrypoint; no V1 hooks or dependency installation needed.
export function createPlugin(run = execute) {
  return {
    id: "agent-comms",
    async setup(ctx) {
      const pending = new Map()
      const ready = new Map()
      const failures = new Map()
      let warned = false

      async function instructions(sessionID) {
        if (ready.has(sessionID)) return ready.get(sessionID)
        // Cache only session-specific lookup instructions, never an identity or
        // spatial prefix. Agents call context for their actual pane before EVERY send.
        if (!pending.has(sessionID)) {
          const operation = (async () => {
            const configured = ctx.options?.python || process.env.AGENT_COMMS_PYTHON
            const python = configured || (process.platform === "win32" ? "py" : "python3")
            const args = [...(!configured && process.platform === "win32" ? ["-3"] : []), helper,
              "instructions", "--format", "text"]
            const result = await run(python, args, { timeout: 15000, maxBuffer: 128 * 1024, windowsHide: true })
            if (!result.stdout.trim()) throw new Error("Empty agent-comms instructions")
            return result.stdout.trim()
          })()
          pending.set(sessionID, operation)
        }
        const operation = pending.get(sessionID)
        try {
          const text = await operation
          ready.set(sessionID, text)
          if (ready.size > 128) ready.delete(ready.keys().next().value)
          return text
        } finally {
          if (pending.get(sessionID) === operation) pending.delete(sessionID)
        }
      }

      await ctx.session.hook("context", async (event) => {
        if (!event.sessionID || (failures.get(event.sessionID) || 0) > Date.now()) return
        try {
          const text = await instructions(event.sessionID)
          failures.delete(event.sessionID)
          const marker = "<agent-comms-session>"
          // Replace our block if a hook is replayed on an existing request draft.
          for (let i = event.system.length - 1; i >= 0; i--) {
            if (event.system[i].type === "text" && event.system[i].text.startsWith(marker)) event.system.splice(i, 1)
          }
          event.system.push({ type: "text", text: `${marker}\n${text}\n</agent-comms-session>` })
        } catch {
          failures.set(event.sessionID, Date.now() + 30000)
          if (!warned) {
            // Never log execFile's error object: it may contain session metadata.
            console.error("agent-comms: identity setup failed; continuing without communications instructions.")
            warned = true
          }
        }
      })
    },
  }
}

export default createPlugin()
