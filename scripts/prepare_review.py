"""Create synthetic review fixtures in a new directory, with no inference or login."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def prepare(root: Path):
    root.mkdir(parents=True, exist_ok=False)
    for kind in ("implementation", "analysis", "research", "document"):
        folder = root / kind
        folder.mkdir()
        if kind == "implementation":
            (folder / "README.md").write_text("Synthetic implementation review fixture. Preserve this file.\n", encoding="utf-8")
            for args in (("init",), ("add", "README.md"),
                         ("-c", "user.name=Plugin Review", "-c", "user.email=review@example.invalid",
                          "-c", "commit.gpgsign=false", "commit", "-m", "Initialize synthetic review fixture")):
                subprocess.run(["git", *args], cwd=folder, check=True, capture_output=True)
        elif kind == "analysis":
            (folder / "data.csv").write_text("value\n2\n4\n6\n", encoding="utf-8")
        else:
            (folder / "source.txt").write_text("The pilot included 12 participants. Eight completed the four-week pilot.\n", encoding="utf-8")
    inputs = {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
              for kind in ("implementation", "analysis", "research", "document")
              for path in (root / kind).iterdir() if path.is_file()}
    (root / "input-hashes.json").write_text(json.dumps(inputs, indent=2) + "\n", encoding="utf-8")
    return {"review_root": str(root.resolve()), "input_hashes": inputs, "model_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="New directory; existing directories are never overwritten")
    print(json.dumps(prepare(parser.parse_args().destination), indent=2))
