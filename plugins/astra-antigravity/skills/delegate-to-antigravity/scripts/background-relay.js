// Paste this helper into a functions.exec cell, followed by one invocation.
// The cell owns the subscription; the parent does independent work after yield.
async function relayAntigravity({launch, receipt, afterSequence = 0}) {
  for (const name of ["receive_delegation_events", "ack_delegation_events"])
    if (!ALL_TOOLS.some(item => item.name === "mcp__luna_gateway__" + name))
      throw new Error("Background gateway tools are unavailable; refresh the plugin before launch");
  function unpack(result) {
    if (result.isError) throw new Error("Gateway rejected the relay request");
    if (result.structuredContent) return result.structuredContent;
    const block = result.content?.find(item => item.type === "text");
    if (!block) throw new Error("Gateway returned no JSON payload");
    return JSON.parse(block.text);
  }
  async function gateway(name, args) {
    const tool = ALL_TOOLS.find(item => item.name === "mcp__luna_gateway__" + name);
    if (!tool) throw new Error("Gateway tool unavailable: " + name);
    return unpack(await tools[tool.name](args));
  }
  if (!receipt) {
    const {tool = "delegate_via_luna", ...args} = launch;
    if (!new Set(["delegate_via_luna", "continue_via_luna"]).has(tool))
      throw new Error("Unsupported background launch");
    const request_id = args.request_id || "relay-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
    store("astra-antigravity:last-launch", {tool, ...args, request_id});
    receipt = await gateway(tool, {...args, request_id, delivery: "background"});
  }
  if (receipt.delivery !== "background" || typeof receipt.delegation_id !== "string")
    throw new Error("Gateway returned no background receipt");
  const key = "astra-delegation:" + receipt.delegation_id;
  const saved = load(key);
  let cursor = Math.max(afterSequence, saved?.sequence || 0, receipt.acknowledged_sequence || 0);
  store(key, {receipt, sequence: cursor});
  text({source: "astra-antigravity", ...receipt});
  await yield_control();
  try {
    // Repair an acknowledgement lost after delivery, before receiving again.
    if (cursor) await gateway("ack_delegation_events", {delegation_id: receipt.delegation_id, sequence: cursor});
    while (true) {
      const batch = await gateway("receive_delegation_events", {delegation_id: receipt.delegation_id, after_sequence: cursor});
      for (const event of batch.events) {
        if (event.sequence <= cursor) continue;
        notify({source: "astra-antigravity", ...event});
        cursor = event.sequence;
        store(key, {receipt, sequence: cursor});
        await gateway("ack_delegation_events", {delegation_id: receipt.delegation_id, sequence: cursor});
      }
      if (batch.delivery_suspended) notify({source: "astra-antigravity", ...batch});
      if (batch.closed) return {delegation_id: receipt.delegation_id, sequence: cursor};
    }
  } catch (_) {
    notify({source: "astra-antigravity", delegation_id: receipt.delegation_id,
            kind: "delivery_disconnected", sequence: cursor,
            summary: "Relay disconnected. Resume delivery from this receipt; do not restart the task."});
    return {delegation_id: receipt.delegation_id, sequence: cursor, delivery_suspended: true};
  }
}
