import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { fileURLToPath } from "node:url"
import { startupContexts } from "./local-context.js"

const execute = promisify(execFile)
const helper = fileURLToPath(new URL("../../agent_comms.py", import.meta.url))

// V2's native object entrypoint; no V1 hooks or dependency installation needed.
export function createPlugin(run = execute, contract) {
  return {
    id: "agent-comms",
    async setup(ctx) {
      const pending = new Map()
      const ready = new Map()
      const failures = new Map()
      let warned = false
      const startup = startupContexts()

      if (ctx.rpc?.register) {
        const StartupContext = contract || (await import("./rpc.js")).StartupContext
        await ctx.rpc.register(StartupContext, {
          capture: async ({ sessionID, ...snapshot }) => {
            // OpenCode disables JSON-Schema regex patterns in RPC input. Check
            // these short, bounded identifiers here instead.
            if (!/^[a-f0-9]{64}$/.test(snapshot.owner) || !/^[a-f0-9]{64}$/.test(snapshot.listener)
              || !/^[A-Za-z][A-Za-z0-9]{0,63}$/.test(snapshot.identity.name)) return { ok: false }
            const session = await ctx.session.get({ sessionID })
            // Child agents must resolve their own tool process's owner. Never
            // use the parent's terminal snapshot or inherit it through storage.
            if (session.parentID) return { ok: false }
            const scope = await instructions(sessionID)
            if (!scope.listener || scope.listener !== snapshot.listener) return { ok: false }
            startup.capture(sessionID, snapshot)
            return { ok: true }
          },
        })
      }

      async function instructions(sessionID) {
        if (ready.has(sessionID)) return ready.get(sessionID)
        // Cache only session-specific lookup instructions, never an identity or
        // spatial prefix. Agents look up their pane, then send with its checked ID.
        if (!pending.has(sessionID)) {
          const operation = (async () => {
            const configured = ctx.options?.python || process.env.AGENT_COMMS_PYTHON
            const python = configured || (process.platform === "win32" ? "py" : "python3")
            const args = [...(!configured && process.platform === "win32" ? ["-3"] : []), helper,
              "instructions", "--startup-scope"]
            const result = await run(python, args, { timeout: 15000, maxBuffer: 128 * 1024, windowsHide: true })
            const data = JSON.parse(result.stdout)
            if (!data.instructions?.trim()) throw new Error("Empty agent-comms instructions")
            return data
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
          const base = (await instructions(event.sessionID)).instructions
          const snapshot = startup.text(event.sessionID)
          const text = snapshot ? `${snapshot}\n\n${base}` : base
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
