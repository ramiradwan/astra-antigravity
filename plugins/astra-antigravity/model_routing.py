"""Small deterministic model policy; reasoning classification belongs to the planner."""

from __future__ import annotations

import os
import re

PROFILES = {
    "sonnet-5.5": {"label": "Claude Sonnet 5.5 (Thinking)", "reasoning": "light",
                   "model": "claude-sonnet-5-5", "pattern": r"claude-sonnet-(?:5-5|5\.5)(?:-thinking)?"},
    "gemini-3.8-high": {"label": "Gemini 3.8 Flash High", "reasoning": "light",
                         "model": "gemini-3.8-flash-high", "pattern": r"gemini-3\.8-flash-high"},
    "argon-high": {"label": "Gemini 4 Argon High", "reasoning": "heavy",
                   "model": "gemini-4-argon-high", "pattern": r"gemini-4-argon-high"},
    "opus-5.5": {"label": "Claude Opus 5.5 (Thinking)", "reasoning": "heavy",
                 "model": "claude-opus-5-5-thinking", "pattern": r"claude-opus-(?:5-5|5\.5)(?:-thinking)?"},
}


def preferences():
    result = {}
    for reasoning, default in (("light", "sonnet-5.5"), ("heavy", "argon-high")):
        key = f"ASTRA_ANTIGRAVITY_{reasoning.upper()}_MODEL"
        value = os.environ.get(key, default).strip()
        if value not in PROFILES or PROFILES[value]["reasoning"] != reasoning:
            raise ValueError(f"Invalid {key}: choose a supported {reasoning} model profile")
        result[reasoning] = value
    return result


def advertised_profiles(catalog):
    result = {}
    for name, profile in PROFILES.items():
        matches = sorted(slug for slug in catalog if re.fullmatch(profile["pattern"], slug))
        # Only return an exact advertised slug; never invent a new-version alias.
        model = profile["model"] if profile["model"] in matches else (matches[0] if matches else None)
        result[name] = {"profile": name, "label": profile["label"], "reasoning": profile["reasoning"],
                        "model": model, "effort": "high", "available": model is not None}
    return result


def select_model(reasoning, catalog, *, model=None):
    if reasoning not in ("light", "heavy"):
        raise ValueError("Reasoning requirement must be light or heavy")
    settings = preferences()
    if model is not None and (model not in PROFILES or PROFILES[model]["reasoning"] != reasoning):
        raise ValueError("Selected model profile does not match the reasoning requirement")
    available = advertised_profiles(catalog)
    preferred = model or settings[reasoning]
    selected = available[preferred]
    notice = None
    # Either light model satisfies the user's policy. Heavy substitutions need
    # an explicit choice, so missing Argon always returns an Opus offer.
    if not selected["available"] and reasoning == "light" and model is None:
        alternative = "gemini-3.8-high" if preferred == "sonnet-5.5" else "sonnet-5.5"
        if available[alternative]["available"]:
            selected = available[alternative]
            notice = f"{available[preferred]['label']} is unavailable; selected {selected['label']}."
    result = {**selected, "preferred_profile": preferred, "notice": notice}
    if selected["available"]:
        return result
    if preferred == "argon-high":
        fallback = available["opus-5.5"]
        message = "Gemini 4 Argon High is unavailable for this account/CLI. Opus 5.5 is offered as the fallback."
        if not fallback["available"]:
            message += " The current CLI does not advertise Opus 5.5 either."
    else:
        other = next(name for name, data in PROFILES.items()
                     if data["reasoning"] == reasoning and name != preferred)
        fallback = available[other]
        message = f"{selected['label']} is unavailable for this account/CLI."
    result.update(requested_model=PROFILES[preferred]["model"], message=message,
                  offered_fallback=fallback,
                  retry={"model": fallback["profile"], "reasoning": reasoning} if fallback["available"] else None)
    return result
