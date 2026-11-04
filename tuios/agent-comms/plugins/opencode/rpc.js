import { Rpc } from "@opencode/plugin/rpc"

export const StartupContext = Rpc.define({
  id: "agent-comms-startup",
  methods: {
    capture: {
      input: {
        type: "object",
        properties: {
          sessionID: { type: "string", minLength: 1 },
          owner: { type: "string", minLength: 64, maxLength: 64 },
          listener: { type: "string", minLength: 64, maxLength: 64 },
          identity: {
            type: "object",
            properties: {
              id: { type: "string" },
              name: { type: "string", minLength: 1, maxLength: 64 },
              epoch: { type: "string", minLength: 1, maxLength: 128 },
              prefix: { type: "string", maxLength: 4096 },
            },
            required: ["name", "epoch", "prefix"],
            additionalProperties: false,
          },
        },
        required: ["sessionID", "owner", "identity", "listener"],
        additionalProperties: false,
      },
      output: { type: "object", properties: { ok: { type: "boolean" } }, required: ["ok"], additionalProperties: false },
      errors: {},
    },
  },
  events: {},
})
