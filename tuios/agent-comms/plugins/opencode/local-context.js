import { execFile } from "node:child_process"
import { promisify } from "node:util"
import { fileURLToPath } from "node:url"

const execute = promisify(execFile)
const helper = fileURLToPath(new URL("../../agent_comms.py", import.meta.url))

// Only call from the terminal client. The shared server's environment is not
// evidence of which terminal is asking for a model response.
export async function localContext(options = {}, run = execute) {
  const configured = options.python || process.env.AGENT_COMMS_PYTHON
  const python = configured || (process.platform === "win32" ? "py" : "python3")
  const args = [...(!configured && process.platform === "win32" ? ["-3"] : []), helper,
    "instructions", "--bootstrap"]
  const result = await run(python, args, { timeout: 15000, maxBuffer: 128 * 1024, windowsHide: true })
  const data = JSON.parse(result.stdout)
  return data.identity && data.owner && data.listener
    ? { identity: data.identity, owner: data.owner, listener: data.listener } : undefined
}

export function startupText(identity) {
  return "## Checked local startup context\n\n" +
    "The OpenCode terminal plugin ran agent-comms context in this terminal before the model started. " +
    "This is a successful same-pane/same-shell lookup. Reuse it immediately without another context call. " +
    "This is a startup snapshot: later checked context or send mismatch results take precedence. " +
    "Never reuse it in a child agent, another pane, or another shell.\n\n" + JSON.stringify(identity)
}

export function startupContexts(limit = 128) {
  const sessions = new Map()
  return {
    capture(sessionID, snapshot) {
      const current = sessions.get(sessionID)
      // A shared conversation opened by different owners is ambiguous. Do not
      // pick either terminal's identity, even if one registers more recently.
      sessions.set(sessionID, current === null || (current && current.owner !== snapshot.owner) ? null : snapshot)
      if (sessions.size > limit) sessions.delete(sessions.keys().next().value)
    },
    text(sessionID) {
      const snapshot = sessions.get(sessionID)
      return snapshot ? startupText(snapshot.identity) : ""
    },
  }
}

export async function setupTerminal(ctx, { local = localContext, effect, contract }) {
  let snapshot
  try {
    // Resolves on the home screen, before a session or prompt exists.
    snapshot = await local(ctx.options)
  } catch {
    return  // Advisory: retain the server's ordinary lookup instructions.
  }
  if (!snapshot) return
  const captured = new Set()
  let disposed = false
  const dispose = ctx.ui.slot({ append: "app", render: () => {
    effect(() => {
      const route = ctx.ui.router.current()
      if (route.type !== "session" || captured.has(route.sessionID)) return
      const sessionID = route.sessionID
      captured.add(sessionID)
      const location = ctx.location ?? ctx.data.location.default()
      void ctx.client.rpc(contract).capture({ sessionID, ...snapshot }, {
        location: { directory: location.directory },
      }).catch(() => {
        if (!disposed) captured.delete(sessionID)
      })
    })
    return null
  } })
  return () => { disposed = true; dispose() }
}
