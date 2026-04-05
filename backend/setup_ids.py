"""Validate setup primary ids (DB row id = manifest id)."""

from __future__ import annotations

import re

SETUP_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,126}$")


def validate_setup_id(setup_id: str) -> str | None:
    """Return error message or None if valid."""
    if not isinstance(setup_id, str) or not setup_id.strip():
        return "Setup id is required"
    s = setup_id.strip()
    if not SETUP_ID_RE.match(s):
        return (
            "Setup id must be 1–127 characters, start with a letter or digit, "
            "and use only letters, digits, underscore, hyphen, and dot"
        )
    return None
