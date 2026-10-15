"""Opt-in delivery adapter for hosts that own a Codex App Server connection.

The host supplies an async JSON-RPC caller and the parent thread id explicitly.
No rollout discovery, guessed thread identity, user-message injection, polling,
extra model, or user configuration mutation is needed. Unsupported hosts leave
the event unacknowledged so another delivery transport can recover it.
"""

from __future__ import annotations

import json


class AppServerDelivery:
    def __init__(self, call, thread_id, server="luna_gateway", save_cursor=None):
        if not thread_id:
            raise ValueError("The host must supply its parent thread id")
        self.call, self.thread_id, self.server = call, thread_id, server
        self.save_cursor = save_cursor

    async def _request(self, method, params):
        response = await self.call(method, params)
        if "error" in response:
            raise RuntimeError(f"App Server rejected {method}: {response['error']}")
        return response.get("result", response)

    async def _gateway(self, tool, arguments):
        result = await self._request("mcpServer/tool/call", {
            "threadId": self.thread_id, "server": self.server,
            "tool": tool, "arguments": arguments})
        # App Server exposes a call-tool result directly on supported versions.
        if result.get("isError"):
            raise RuntimeError("Gateway rejected the relay request")
        if result.get("structuredContent"):
            return result["structuredContent"]
        content = result.get("content") or []
        text = next((item["text"] for item in content if item.get("type") == "text"), None)
        if text is None:
            raise RuntimeError("App Server returned no gateway JSON payload")
        return json.loads(text)

    async def deliver(self, event):
        # This is tool data, not an invented user/assistant message. Codex queues
        # it into a regular active turn, or starts a turn when the thread is idle.
        return await self._request("turn/start", {
            "threadId": self.thread_id, "input": [],
            "toolOutput": {"name": "antigravity_event", "namespace": None,
                           "output": json.dumps(event, separators=(",", ":"), ensure_ascii=True)}})

    async def relay(self, delegation_id, after_sequence=0):
        cursor = after_sequence
        if cursor:
            await self._gateway("ack_delegation_events", {"delegation_id": delegation_id, "sequence": cursor})
        while True:
            batch = await self._gateway("receive_delegation_events", {
                "delegation_id": delegation_id, "after_sequence": cursor})
            for event in batch["events"]:
                if event["sequence"] <= cursor:
                    continue
                await self.deliver(event)
                cursor = event["sequence"]
                if self.save_cursor:
                    await self.save_cursor(delegation_id, cursor)
                await self._gateway("ack_delegation_events", {"delegation_id": delegation_id, "sequence": cursor})
            if batch.get("delivery_suspended"):
                await self.deliver(batch)
            if batch["closed"]:
                return cursor
