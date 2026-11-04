import { Plugin } from "@opencode/plugin/tui"
import { createEffect } from "solid-js"
import { setupTerminal } from "./local-context.js"
import { StartupContext } from "./rpc.js"

export default Plugin.define({
  id: "agent-comms-terminal",
  setup(ctx) {
    return setupTerminal(ctx, { effect: createEffect, contract: StartupContext })
  },
})
