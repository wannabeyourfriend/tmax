"""Robust JSON extraction from Terminus-2 assistant messages.

Implements a 3-strategy cascade:
  Strategy 1 – direct ``json.loads`` (content is pure JSON).
  Strategy 2 – brace-matching to locate outermost ``{…}`` in mixed prose+JSON.
  Strategy 3 – fix common LLM JSON errors (trailing commas) then retry parse.
"""

from __future__ import annotations

import json
import re


def extract_json_from_content(content: str) -> tuple[dict | None, str, int]:
    """Extract a JSON object from an assistant message.

    Returns
    -------
    parsed : dict | None
        The parsed JSON dict, or ``None`` if all strategies fail.
    prose : str
        Any text surrounding the JSON blob (empty when content is pure JSON).
    strategy : int
        Which strategy succeeded (1, 2, 3) or 0 on failure.
    """
    content = content.strip()

    # Strategy 1: direct parse
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed, "", 1
    except json.JSONDecodeError:
        pass

    # Strategy 2: brace-match the outermost {…}
    start_idx = content.find("{")
    if start_idx == -1:
        return None, content, 0

    end_idx = _find_matching_brace(content, start_idx)
    if end_idx is None:
        return None, content, 0

    json_str = content[start_idx : end_idx + 1]
    prose = _surrounding_prose(content, start_idx, end_idx)

    try:
        parsed = json.loads(json_str)
        if isinstance(parsed, dict):
            return parsed, prose, 2
    except json.JSONDecodeError:
        pass

    # Strategy 3: attempt common fixes then re-parse
    fixed = _fix_common_json_errors(json_str)
    try:
        parsed = json.loads(fixed)
        if isinstance(parsed, dict):
            return parsed, prose, 3
    except json.JSONDecodeError:
        pass

    return None, content, 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_matching_brace(text: str, start: int) -> int | None:
    """Return the index of the ``}`` that closes the ``{`` at *start*."""
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(text)):
        c = text[i]
        if escape_next:
            escape_next = False
            continue
        if c == "\\" and in_string:
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _surrounding_prose(content: str, json_start: int, json_end: int) -> str:
    before = content[:json_start].strip()
    after = content[json_end + 1 :].strip()
    parts = [p for p in (before, after) if p]
    return "\n".join(parts)


def _fix_common_json_errors(json_str: str) -> str:
    """Best-effort fixes for common LLM JSON generation mistakes."""
    # Trailing commas before } or ]
    json_str = re.sub(r",\s*([}\]])", r"\1", json_str)
    return json_str
