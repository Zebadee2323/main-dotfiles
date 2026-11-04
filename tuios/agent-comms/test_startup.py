"""Startup snapshots and inline roles, using isolated state and listeners."""
import json
import os
import shutil
import subprocess
from unittest.mock import patch

import agent_comms as app
import pane_identity
from test_agent_comms import Fixture, ROOT


class InlineRoleTests(Fixture):
    def role(self, env):
        return pane_identity.lookup(self.paths, env)[1].get("agent_role")

    def test_context_sets_preserves_and_clears_role_in_same_allocation(self):
        env = self.pane_env("role-owner")
        config = app.load_config(self.paths.config)
        with patch.object(app, "checked_context", return_value={}), patch.object(app, "update_pane_title"):
            first = app.identify(self.paths, config, env, agent_role="worker")
            self.assertEqual(self.role(env), "worker")
            self.assertEqual(app.identify(self.paths, config, env), first)
            self.assertEqual(self.role(env), "worker")
            app.identify(self.paths, config, env, agent_role="")
            self.assertEqual(self.role(env), "")
            with self.assertRaises(app.CommsError):
                app.identify(self.paths, config, env, agent_role="bad\nrole")
            self.assertEqual(self.role(env), "")

    def test_context_cli_role_option_uses_listener_and_rejects_clear_combination(self):
        with patch.object(app, "listener_context", return_value={"name": "Alice"}) as lookup:
            result = app.main(["--config", str(self.paths.config), "context", "--agent-role", "reviewer"])
        self.assertEqual(result, 0)
        self.assertEqual(lookup.call_args.kwargs, {"agent_role": "reviewer"})
        result = self.cli("context", "--clear", "--agent-role", "worker")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be combined", result.stderr)

    def test_send_role_is_persisted_only_with_valid_message_and_snapshotted(self):
        sender = self.sender("Alice")
        with self.paths.config.open("a") as stream:
            stream.write('\n[user]\nname = "Oli"\n')
        self.start_server()
        def request(**data):
            _, stream = self.request("send", **data)
            return json.loads(stream.readline())
        response = request(**sender, msg="Working.", agent_role="worker", role="responder")
        self.assertTrue(response["ok"], response)
        self.assertEqual(self.role(sender["pane_env"]), "worker")
        records = app.message_history.read(self.paths.state)
        self.assertEqual(records[-1]["agent_role"], "worker")
        for change in ({"name": "Bob", "agent_role": "supervisor"},
                       {"msg": "", "agent_role": "supervisor"},
                       {"agent_role": "bad\nrole"}):
            data = {**sender, "msg": "Working.", "agent_role": "worker", **change}
            response = request(**data)
            self.assertFalse(response["ok"], response)
            self.assertEqual(self.role(sender["pane_env"]), "worker")
        response = request(**sender, msg="Done.", agent_role="")
        self.assertTrue(response["ok"], response)
        self.assertEqual(self.role(sender["pane_env"]), "")

    def test_send_cli_preserves_exchange_role_and_forwards_persistent_role(self):
        env = self.pane_env("cli-owner")
        sender = self.sender("Alice", "cli-owner")
        with patch.dict(os.environ, env), patch.object(app, "checked_context", return_value={}), \
             patch.object(app, "caller_binding", return_value=(sender["epoch"], "Alice")), \
             patch.object(app, "send", return_value={"message_id": "accepted"}) as send:
            result = app.main(["--config", str(self.paths.config), "send", "--name", "Alice",
                               "--epoch", sender["epoch"], "--msg", "Working.",
                               "--agent-role", "worker", "--role", "responder"])
        self.assertEqual(result, 0)
        self.assertEqual(send.call_args.args[-1]["agent_role"], "worker")
        self.assertEqual(send.call_args.args[-1]["role"], "responder")

    def test_bootstrap_fallback_keeps_instructions_and_plain_mode_never_allocates(self):
        result = self.cli("instructions", "--bootstrap")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn("instructions", data)
        self.assertNotIn("identity", data)
        self.assertIn("startup context unavailable", result.stderr)
        self.assertFalse(self.paths.state.exists())


class OpenCodeStartupTests(Fixture):
    def node(self, script):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable")
        result = subprocess.run([node, "--input-type=module"], input=script,
                                env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_server_injects_only_local_root_snapshot_and_never_looks_up_server_owner(self):
        self.node(f'''
import assert from "node:assert/strict";
import {{ createPlugin }} from {json.dumps((ROOT / "plugins/opencode/index.js").as_uri())};
let hooks = {{}}, capture, calls = 0;
await createPlugin(async (_, args) => {{
  calls++; assert.deepEqual(args.slice(-2), ["instructions", "--startup-scope"]);
  return {{stdout: JSON.stringify({{instructions:"Communication instructions", listener:"a".repeat(64)}})}};
}}, {{}}).setup({{
  rpc: {{register: async (_, methods) => {{capture = methods.capture;}}}},
  session: {{get: async ({{sessionID}}) => ({{parentID: sessionID === "child" ? "root" : undefined}}),
    hook: async (name, fn) => {{hooks[name] = fn;}}}},
}});
const snapshot = {{owner: "b".repeat(64), listener:"a".repeat(64), identity: {{name: "Alice", epoch: "epoch", prefix: "Alice here."}}}};
assert.deepEqual(await capture({{sessionID:"remote", ...snapshot, listener:"c".repeat(64)}}), {{ok:false}});
await capture({{sessionID: "root", ...snapshot}});
const event = {{sessionID: "root", system: []}};
await hooks.context(event); await hooks.context(event);
assert.equal(event.system.length, 1); assert.equal(calls, 2);
assert.match(event.system[0].text, /"name":"Alice"/);
assert.deepEqual(await capture({{sessionID: "child", ...snapshot}}), {{ok:false}});
const child = {{sessionID: "child", system: []}};
await hooks.context(child); assert.doesNotMatch(child.system[0].text, /Checked local startup/);
await capture({{sessionID: "root", ...snapshot, owner: "d".repeat(64)}});
await hooks.context(event); assert.doesNotMatch(event.system[0].text, /Checked local startup/);
''')

    def test_terminal_setup_resolves_before_prompt_then_registers_route_and_disposes(self):
        self.node(f'''
import assert from "node:assert/strict";
import {{ setupTerminal }} from {json.dumps((ROOT / "plugins/opencode/local-context.js").as_uri())};
let route = {{type:"home"}}, render, effect, lookups=0, disposed=0;
const captures = [];
const snapshot = {{owner:"owner",identity:{{name:"Alice",epoch:"epoch",prefix:""}}}};
const ctx = {{location:{{directory:"/local/worktree"}},
  ui: {{router:{{current:()=>route}}, slot: options => {{render=options.render; return ()=>disposed++;}}}},
  client: {{rpc: () => ({{capture: async (value, opts) => {{
    assert.deepEqual(opts.location,{{directory:"/local/worktree"}}); captures.push(value); return {{ok:true}};
  }}}})}}}};
const cleanup = await setupTerminal(ctx, {{local: async () => {{lookups++; return snapshot;}},
  effect: fn => {{effect=fn; fn();}}, contract:{{}}}});
assert.equal(lookups,1); assert.equal(captures.length,0);
render(); route={{type:"session",sessionID:"root"}}; effect(); effect();
assert.equal(captures.length,1); assert.equal(captures[0].sessionID,"root");
assert.deepEqual(captures[0].identity,snapshot.identity);
route={{type:"session",sessionID:"second"}}; effect(); assert.equal(captures.length,2);
cleanup(); assert.equal(disposed,1);
assert.equal(await setupTerminal(ctx, {{local:async()=>{{throw Error("offline");}}}}), undefined);
''')

    def test_local_client_bootstrap_and_session_cache_are_owner_scoped_and_bounded(self):
        self.node(f'''
import assert from "node:assert/strict";
import {{ localContext, startupContexts }} from {json.dumps((ROOT / "plugins/opencode/local-context.js").as_uri())};
const snapshot = {{owner: "owner-a", listener:"local-listener", identity: {{name: "Alice", epoch: "epoch", prefix: "Alice here."}}}};
let calls = 0;
const data = await localContext({{python: "local-python"}}, async (python, args, opts) => {{
  calls++; assert.equal(python, "local-python");
  assert.deepEqual(args.slice(-2), ["instructions", "--bootstrap"]);
  assert.equal(opts.timeout, 15000);
  return {{stdout: JSON.stringify(snapshot)}};
}});
assert.equal(calls, 1); assert.deepEqual(data, snapshot);
assert.equal(await localContext({{}}, async () => ({{stdout: '{{"instructions":"fallback"}}'}})), undefined);
const cache = startupContexts(2);
cache.capture("root-a", snapshot);
assert.match(cache.text("root-a"), /"name":"Alice"/);
assert.match(cache.text("root-a"), /later checked context/);
assert.equal(cache.text("child"), "");
cache.capture("root-a", {{...snapshot, owner: "owner-b"}});
assert.equal(cache.text("root-a"), "");
cache.capture("root-a", snapshot); assert.equal(cache.text("root-a"), "");
cache.capture("root-b", snapshot); cache.capture("root-c", snapshot);
assert.equal(cache.text("root-a"), ""); assert.notEqual(cache.text("root-c"), "");
''')
