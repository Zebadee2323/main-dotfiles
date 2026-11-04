import { Plugin } from "@opencode/plugin/tui";
import { createEffect, createRoot } from "solid-js";
import { Bridge } from "./bridge.js";
import { ContextRefresh } from "./context-refresh.js";

export default Plugin.define({
  id: "tuios-v2",
  setup(ctx) {
    // Requiring an address prevents accidentally reporting the focused pane
    // when OpenCode is launched outside TUIOS.
    if (!process.env.TUIOS_PANE_ID) return;
    const bridge = new Bridge(ctx);
    const refresh = new ContextRefresh(undefined, () => ctx.ui.toast.show({
      title: "TUIOS", message: "agent-comms title refresh unavailable; conversation mirroring is unaffected.", variant: "warning",
    }));
    const stop = ctx.data.listen(({ details }) => bridge.event(details));
    const dispose = createRoot((dispose) => {
      createEffect(() => {
        const route = ctx.ui.router.current();
        bridge.select(route.type === "session" ? route.sessionID : undefined);
      });
      createEffect(() => {
        const route = ctx.ui.router.current();
        const sessionID = route.type === "session" ? route.sessionID : undefined;
        refresh.select(sessionID, sessionID ? ctx.data.session.get(sessionID)?.title : undefined);
      });
      return dispose;
    });
    return async () => {
      dispose();
      stop();
      await Promise.all([bridge.close(), refresh.close()]);
    };
  },
});
