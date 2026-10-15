"""Non-inference exact slug model availability from bounded 'agy models'."""

from __future__ import annotations

import json
import re
from typing import Callable

from command_probes import run_probe


def parse_model_slugs(raw_output: str) -> set[str]:
    """Parse exact model slugs from 'agy models' output (JSON or tabular text)."""
    if not raw_output or not raw_output.strip():
        return set()

    text = raw_output.strip()
    # Machine-readable rows must advertise an exact enabled identifier.
    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = next((data[key] for key in ("models", "data", "items")
                         if isinstance(data.get(key), list)), [])
        if isinstance(data, list):
            slugs = set()
            for row in data:
                if isinstance(row, str):
                    slugs.add(row.strip())
                elif isinstance(row, dict) and row.get("available") is not False and row.get("enabled") is not False:
                    if str(row.get("status", "")).lower() in {"unavailable", "disabled"}:
                        continue
                    for key in ("slug", "id", "model", "name"):
                        value = row.get(key)
                        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9.]+)+", value):
                            slugs.add(value)
            return slugs

    # A model is advertised in the first column, not merely mentioned in prose.
    slugs = set()
    for line in text.splitlines():
        match = re.match(r"^\s*([a-z][a-z0-9]*(?:-[a-z0-9.]+)+)(?:\s|$)", line)
        if match:
            if not re.search(r"\b(?:unavailable|disabled)\b", line, re.I):
                slugs.add(match.group(1))
    return slugs



def get_available_models(find_agy_fn: Callable[[], str], timeout: float = 10.0) -> set[str]:
    agy = find_agy_fn()
    try:
        completed = run_probe([agy, "models"], timeout=timeout)
    except Exception as exc:
        raise RuntimeError(f"Failed to query 'agy models': {exc}") from exc
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"'agy models' exited with code {completed.returncode}: {err[:500]}")
    return parse_model_slugs(completed.stdout)


def check_model_availability(model: str, find_agy_fn: Callable[[], str], timeout: float = 10.0) -> bool:
    slugs = get_available_models(find_agy_fn, timeout=timeout)
    return model in slugs
