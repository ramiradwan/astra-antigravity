import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def unpack(result):
    if result.isError:
        raise AssertionError(result.content)
    return json.loads(next(item.text for item in result.content if item.type == "text"))


class BackgroundMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_server_disconnect_restart_and_ack_recovery_do_not_reexecute_work(self):
        with tempfile.TemporaryDirectory(prefix="astra-background-mcp-") as raw:
            tmp = Path(raw)
            repo = tmp / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                            "commit", "--allow-empty", "-qm", "base"], check=True, capture_output=True)
            env = {**os.environ, "ASTRA_ANTIGRAVITY_STATE_DIR": str(tmp / "state"),
                   "ASTRA_ANTIGRAVITY_WORKTREE_ROOT": str(tmp / "worktrees"),
                   "ASTRA_FAKE_AGY_RELEASE_FILE": str(tmp / "release"),
                   "ASTRA_TEST_PLANNER_COUNT": str(tmp / "planner-count"),
                   "ASTRA_TEST_RECEIPT_FILE": str(tmp / "receipt"),
                   "ASTRA_TEST_PARENT_WORK_FILE": str(tmp / "parent-work")}
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(ROOT / "tests/fixtures/background_gateway_server.py")], env=env)
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    receipt = unpack(await session.call_tool("delegate_via_luna", {
                        "task": "Implement", "workspace": str(repo), "request_id": "mcp-recovery"}))
                    self.assertEqual(receipt["delivery"], "background")
                    self.assertFalse(receipt["terminal"])
                    await session.call_tool("record_parent_work", {})
                    blocker = unpack(await session.call_tool("receive_delegation_events", {
                        "delegation_id": receipt["delegation_id"]}))
                    self.assertEqual(blocker["events"][0]["kind"], "needs_decision")
                    pending = asyncio.create_task(session.call_tool("receive_delegation_events", {
                        "delegation_id": receipt["delegation_id"], "after_sequence": 1}))
                    await asyncio.sleep(0)
                    await asyncio.wait_for(session.send_ping(), 2)
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                    (tmp / "release").write_text("release result")
                    completed = unpack(await session.call_tool("receive_delegation_events", {
                        "delegation_id": receipt["delegation_id"], "after_sequence": 1}))
                    self.assertEqual(completed["events"][0]["kind"], "completed")
                    terminal = unpack(await session.call_tool("read_delegation_result", {
                        "delegation_id": receipt["delegation_id"]}))
                    self.assertTrue(terminal["implementation"]["structured_report_valid"])
            # This is a NEW MCP server/process/owner using only durable evidence.
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    replay = unpack(await session.call_tool("receive_delegation_events", {
                        "delegation_id": receipt["delegation_id"]}))
                    self.assertEqual(replay["events"], blocker["events"])
                    await session.call_tool("ack_delegation_events", {"delegation_id": receipt["delegation_id"], "sequence": 1})
                    replay_final = unpack(await session.call_tool("receive_delegation_events", {
                        "delegation_id": receipt["delegation_id"]}))
                    self.assertEqual(replay_final, completed)
                    saved = unpack(await session.call_tool("read_delegation_result", {"delegation_id": receipt["delegation_id"]}))
                    self.assertEqual(saved, terminal)
                    await session.call_tool("ack_delegation_events", {"delegation_id": receipt["delegation_id"], "sequence": 2})
                    self.assertEqual(unpack(await session.call_tool("receive_delegation_events", {
                        "delegation_id": receipt["delegation_id"]}))["events"], [])
                    recovered = unpack(await session.call_tool("inspect_delegation", {
                        "workspace": str(repo), "request_id": "mcp-recovery"}))
                    self.assertEqual(recovered["status"], "completed")
                    self.assertEqual(recovered["acknowledged_sequence"], 2)
                    self.assertEqual((tmp / "planner-count").read_text().splitlines(), ["plan"])
                    worktree = Path(saved["implementation"]["worktree"]["path"])
                    self.assertNotEqual(worktree, repo)
                    self.assertEqual((worktree / "native_result.txt").read_text(), "turn 1\n")
                    subprocess.run(["git", "-C", str(worktree), "add", "native_result.txt"], check=True, capture_output=True)
                    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                                    "commit", "-qm", "verified fixture"], check=True, capture_output=True)
                    cleanup = unpack(await session.call_tool("cleanup_task_worktree", {"task_id": saved["task_id"]}))
                    self.assertTrue(cleanup["cleaned"])
                    self.assertFalse(worktree.exists())


if __name__ == "__main__":
    unittest.main()
