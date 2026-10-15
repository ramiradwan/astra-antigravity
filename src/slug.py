"""Utilities for generating URL-safe and kebab-case slugs."""

import re
import unicodedata

__all__ = ["slugify"]


def slugify(text: str) -> str:
    """Convert a string to a lower-case kebab-case slug, stripping punctuation.

    Args:
        text: The string to convert.

    Returns:
        The kebab-case slug.

    Raises:
        TypeError: If text is not a string.
    """
    if not isinstance(text, str):
        raise TypeError(f"slugify expected str, got {type(text).__name__}")

    # Map unicode dashes to standard ASCII hyphen
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)

    # Normalize unicode to decomposed form and remove non-ASCII characters
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")

    # Convert to lowercase
    text = text.lower()

    # Treat underscores as word separators (hyphens in kebab-case)
    text = text.replace("_", "-")

    # Strip punctuation (keep only alphanumeric characters, whitespace, and hyphens)
    text = re.sub(r"[^\w\s-]", "", text)

    # Collapse consecutive whitespace and hyphens into a single hyphen
    text = re.sub(r"[-\s]+", "-", text)

    # Strip leading and trailing hyphens
    return text.strip("-")
