"""Bundled-release entry point; loads only the matching packaged Python runtime."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import runpy
import site
import sys
import sysconfig


def runtime_tag():
    tag = f"{sys.implementation.cache_tag}-{sysconfig.get_platform()}"
    if sysconfig.get_config_var("Py_GIL_DISABLED"):
        tag += "-free-threaded"
    if "d" in getattr(sys, "abiflags", ""):
        tag += "-debug"
    return tag


def main():
    root = Path(__file__).resolve().parent
    if sys.argv[1:] != ["luna_gateway.py"]:
        raise RuntimeError("Bundled runtime accepts only the Luna gateway target")
    runtime = root / "runtime" / runtime_tag()
    info_file = runtime / "runtime-info.json"
    if not info_file.is_file():
        raise RuntimeError(
            f"This archive has no runtime for {runtime_tag()}. "
            "Use a listed CPython version/platform or the standard uv-bootstrap package."
        )
    info = json.loads(info_file.read_text(encoding="utf-8"))
    if info.get("tag") != runtime_tag() or info.get("sdk_version") != "1.29.1":
        raise RuntimeError("Bundled runtime metadata does not match the interpreter/SDK")
    packages = runtime / "site-packages"
    # -I excludes the repository, user site and PYTHONPATH. Trusted package .pth
    # files are still needed for native dependencies such as pywin32.
    sys.path.insert(0, str(packages))
    site.addsitedir(str(packages))
    sys.path.insert(0, str(root))
    import mcp
    if (not Path(mcp.__file__).resolve().is_relative_to(packages.resolve())
            or importlib.metadata.version("mcp") != "1.29.1"):
        raise RuntimeError("Gateway did not load its packaged MCP SDK")
    runpy.run_path(str(root / "luna_gateway.py"), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as exc:
        print(f"astra-antigravity bundled startup: {exc}", file=sys.stderr)
        raise SystemExit(2)
