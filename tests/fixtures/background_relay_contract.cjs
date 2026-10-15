const assert = require("node:assert/strict");
const fs = require("node:fs");
const relay = new Function(fs.readFileSync(process.argv[2], "utf8") + ";return relayAntigravity;")();

const names = ["delegate_via_luna", "continue_via_luna", "receive_delegation_events", "ack_delegation_events"];
const wire = value => ({content: [{type: "text", text: JSON.stringify(value)}]});
const receipt = {delegation_id: "delegation-test", delivery: "background", acknowledged_sequence: 0};
const events = [
  {delegation_id: receipt.delegation_id, event_id: "test:1", sequence: 1, kind: "needs_decision", terminal: false},
  {delegation_id: receipt.delegation_id, event_id: "test:2", sequence: 2, kind: "completed", terminal: true},
];
function setup(handler) {
  const state = new Map(), outputs = [], calls = [];
  let yielded;
  const yieldPromise = new Promise(resolve => { yielded = resolve; });
  global.ALL_TOOLS = names.map(name => ({name: "mcp__luna_gateway__" + name}));
  global.tools = Object.fromEntries(names.map(name => ["mcp__luna_gateway__" + name, async args => {
    calls.push([name, args]);
    return wire(await handler(name, args, outputs));
  }]));
  global.store = (key, value) => state.set(key, value);
  global.load = key => state.get(key);
  global.text = value => outputs.push({channel: "receipt", value});
  global.notify = value => outputs.push({channel: "push", value});
  global.yield_control = async () => { yielded(); };
  return {state, outputs, calls, yieldPromise};
}

async function main() {
  let receive;
  const first = setup(async (name, args, outputs) => {
    if (name === "delegate_via_luna") return receipt;
    if (name === "receive_delegation_events") return await new Promise(resolve => { receive = resolve; });
    assert(outputs.some(output => output.channel === "push" && output.value.sequence === args.sequence));
    return {};
  });
  const running = relay({launch: {task: "Implement", workspace: "/repo"}});
  await first.yieldPromise;
  await new Promise(resolve => setImmediate(resolve));
  const parentIndependentWork = "parent completed an independent operation";
  assert(parentIndependentWork);
  assert.equal(first.outputs.filter(output => output.channel === "push").length, 0);
  assert.equal(first.calls[0][1].delivery, "background");
  assert(first.calls[0][1].request_id);
  receive({events: [events[0]], closed: false});
  await new Promise(resolve => setImmediate(resolve));
  receive({events: [events[1]], closed: true});
  await running;
  assert.deepEqual(first.outputs.filter(output => output.channel === "push").map(output => output.value.kind), ["needs_decision", "completed"]);
  assert.deepEqual(first.calls.map(call => call[0]), ["delegate_via_luna", "receive_delegation_events", "ack_delegation_events", "receive_delegation_events", "ack_delegation_events"]);

  let failAck = true;
  const reconnect = setup(async (name, args) => {
    if (name === "receive_delegation_events") return {events: [events[args.after_sequence]], closed: args.after_sequence === 1};
    if (failAck) { failAck = false; throw new Error("lost acknowledgement connection"); }
    return {};
  });
  await relay({receipt});
  assert.equal(reconnect.state.get("astra-delegation:" + receipt.delegation_id).sequence, 1);
  await relay({receipt});
  assert.deepEqual(reconnect.outputs.filter(output => output.value.event_id).map(output => output.value.event_id), ["test:1", "test:2"]);
  assert.equal(reconnect.calls[2][0], "ack_delegation_events");
  assert.equal(reconnect.calls[2][1].sequence, 1);

  const missing = setup(async () => { throw new Error("must not launch"); });
  global.ALL_TOOLS = [{name: "mcp__luna_gateway__delegate_via_luna"}];
  await assert.rejects(relay({launch: {task: "implement"}}), /refresh the plugin/);
  assert.equal(missing.calls.length, 0);
  console.log(JSON.stringify({parent_work_before_completion: true, root_status_checks: 0,
                              pushed_events: 2, lost_ack_recovered_without_duplicate: true}));
}
main().catch(error => { console.error(error); process.exitCode = 1; });
