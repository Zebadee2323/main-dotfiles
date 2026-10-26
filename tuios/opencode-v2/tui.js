import { Plugin } from "@opencode/plugin/tui";
import { createEffect, createRoot } from "solid-js";
import { Bridge } from "./bridge.js";

export default Plugin.define({
  id: "tuios-v2",
  setup(ctx) {
    // Requiring an address prevents accidentally reporting the focused pane
    // when OpenCode is launched outside TUIOS.
    if (!process.env.TUIOS_PANE_ID) return;
    const bridge = new Bridge(ctx);
    const stop = ctx.data.listen(({ details }) => bridge.event(details));
    const dispose = createRoot((dispose) => {
      createEffect(() => {
        const route = ctx.ui.router.current();
        bridge.select(route.type === "session" ? route.sessionID : undefined);
      });
      return dispose;
    });
    return async () => {
      dispose();
      stop();
      await bridge.close();
    };
  },
});
