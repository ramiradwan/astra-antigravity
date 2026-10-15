"""Non-inference CLI fixture: every probe must see EOF on stdin."""

import os
from pathlib import Path
import sys
import time

role, verb = sys.argv[1:3]
assert sys.stdin.buffer.read() == b"", "probe inherited the MCP input stream"
with Path(os.environ["ASTRA_PROBE_CALLS"]).open("a", encoding="utf-8") as calls:
    calls.write(f"{role}:{verb}\n")
if role == "codex" and os.environ.get("ASTRA_PROBE_SLOW"):
    Path(os.environ["ASTRA_PROBE_STARTED"]).write_text("started", encoding="utf-8")
    time.sleep(2)
if verb == "--version":
    print(f"fake-{role} 1.0")
elif role == "agy" and verb == "models":
    if os.environ.get("ASTRA_PROBE_OVERSIZED"):
        print("gemini-4-argon-high\n" * 10000)
    else:
        print("gemini-3.8-flash-high\tGemini 3.8 Flash High")
else:
    raise AssertionError(f"Unexpected command: {role} {verb}")
