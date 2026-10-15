"""Locate packaged plugin resources in source and self-contained workers."""
from pathlib import Path
import sys


def plugin_directory():
    frozen = getattr(sys, "_MEIPASS", None)
    return Path(frozen) / "plugins/astra-antigravity" if frozen else Path(__file__).resolve().parent
