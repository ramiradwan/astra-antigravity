import asyncio
import json
import sys
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "mcp_await_server.py"


class McpAwaitContractTests(unittest.TestCase):
    def test_one_tool_call_streams_progress_before_single_terminal_result(self):
        async def run():
            ordering = []
            progress = []
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(FIXTURE)],
            )

            async def on_progress(value, total, message):
                progress.append((value, total, message))
                ordering.append(("progress", value, message))

            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "await_task",
                        {"task_id": "task-wire"},
                        progress_callback=on_progress,
                    )
                    ordering.append(("result", None, None))
                    return result, progress, ordering

        result, progress, ordering = asyncio.run(run())

        self.assertFalse(result.isError)
        self.assertEqual(
            progress,
            [
                (1.0, None, "Inspecting repository"),
                (2.0, None, "Running pytest -q"),
            ],
        )
        self.assertEqual([row[0] for row in ordering], ["progress", "progress", "result"])

        # FastMCP 1.29.1 serializes an untyped dict return as JSON text content;
        # structuredContent is reserved for tools with an advertised output schema.
        self.assertIsNone(result.structuredContent)
        self.assertEqual(len(result.content), 1)
        terminal = json.loads(result.content[0].text)
        self.assertEqual(terminal["task_id"], "task-wire")
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(
            terminal["implementation"]["summary"],
            "wire contract complete",
        )


if __name__ == "__main__":
    unittest.main()
