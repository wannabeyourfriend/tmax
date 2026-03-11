"""Pure builder functions that convert parsed Terminus-2 fields into SWE-agent
message components.

Every function is deterministic and side-effect-free.
"""

from __future__ import annotations

import hashlib
import re

_SHELL_PROMPT_RE = re.compile(
    r"(?:\([^)]+\)\s+)?"          # optional venv prefix like (venv)
    r"(?:root|[\w]+)"             # username
    r"@[\w][\w.-]{6,}"           # @hostname (UUID or long hostname, >=7 chars)
    r":[^\n#$]*[#$]\s?"          # :/path# or :/path$
)


# ---------------------------------------------------------------------------
# Thought / reasoning_content
# ---------------------------------------------------------------------------

def build_reasoning_content(parsed_json: dict, surrounding_prose: str) -> str:
    """Combine analysis, plan, and surrounding prose into a single string
    that will populate the ``reasoning_content`` field (rendered as
    ``<think>…</think>`` by the Qwen3.5 chat template).
    """
    parts: list[str] = []
    if surrounding_prose:
        parts.append(surrounding_prose)
    analysis = parsed_json.get("analysis", "").strip()
    if analysis:
        parts.append(analysis)
    plan = parsed_json.get("plan", "").strip()
    if plan:
        parts.append(plan)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------

def build_tool_calls(
    parsed_json: dict,
    conversation_id: str,
    turn_index: int,
) -> list[dict]:
    """Convert the ``commands`` array into a single ``bash`` tool_call.

    Multiple commands are newline-joined (persistent shell sends each line
    sequentially).  Wait-only entries (empty keystrokes with duration) are
    dropped.  Special keystrokes ``C-c`` / ``C-d`` are preserved verbatim.

    Returns an empty list when there are no actionable commands.
    """
    commands = parsed_json.get("commands", [])
    if not commands:
        return []

    parts: list[str] = []
    for cmd in commands:
        if isinstance(cmd, str):
            ks = cmd
        else:
            ks = cmd.get("keystrokes", "")
        if not ks.strip():
            continue
        if ks.strip() in ("C-c", "C-d"):
            parts.append(ks.strip())
            continue
        cleaned = ks.rstrip("\n")
        if cleaned:
            parts.append(cleaned)

    if not parts:
        return []

    command_string = "\n".join(parts)
    tool_call_id = _deterministic_id(conversation_id, f"turn{turn_index}")

    return [
        {
            "function": {
                "name": "bash",
                "arguments": {"command": command_string},
            },
            "id": tool_call_id,
            "type": "function",
        }
    ]


# ---------------------------------------------------------------------------
# Tool results
# ---------------------------------------------------------------------------

def build_tool_result(user_content: str, tool_call_id: str) -> dict:
    """Convert a Terminus-2 user message (terminal output) into a tool result.

    Strips harness framing (``Current terminal state:``, ``New Terminal
    Output:``, the "Are you sure?" confirmation prompt), container shell
    prompts (``root@<uuid>:/path#``), and trailing blank lines.
    Returns flat-string ``content`` for Qwen3.5 compatibility.
    """
    text = user_content

    prefix_a = "Current terminal state:\n"
    if text.startswith(prefix_a):
        text = text[len(prefix_a) :]

    prefix_b = "New Terminal Output:\n"
    if text.startswith(prefix_b):
        text = text[len(prefix_b) :]

    confirmation = "Are you sure you want to mark the task as complete?"
    if confirmation in text:
        text = text[: text.index(confirmation)]

    text = _SHELL_PROMPT_RE.sub("", text)

    # Collapse runs of blank lines left after prompt stripping
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    return {
        "role": "tool",
        "content": text,
        "tool_call_ids": [tool_call_id],
    }


SUBMIT_COMMAND = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


# ---------------------------------------------------------------------------
# Submit handling
# ---------------------------------------------------------------------------

def build_submit_messages(
    parsed_json: dict,
    conversation_id: str,
    turn_index: int,
    reasoning_content: str,
) -> list[dict]:
    """Return a ``bash`` tool_call that echoes the submit marker when
    ``task_complete`` is ``True``.  Returns an empty list otherwise.

    If the turn also had commands, the reasoning is already on the preceding
    bash assistant message, so it is not duplicated here.
    """
    if not parsed_json.get("task_complete", False):
        return []

    submit_id = _deterministic_id(conversation_id, f"submit{turn_index}")

    msg: dict = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "function": {
                    "name": "bash",
                    "arguments": {"command": SUBMIT_COMMAND},
                },
                "id": submit_id,
                "type": "function",
            }
        ],
    }

    if not parsed_json.get("commands", []):
        msg["reasoning_content"] = reasoning_content

    return [msg]


# ---------------------------------------------------------------------------
# Harness error detection
# ---------------------------------------------------------------------------

_HARNESS_ERROR_MARKERS = (
    "ERROR: No valid JSON found",
    "No valid JSON found in response",
    "No valid JSON object found",
)


def is_harness_error(user_content: str) -> bool:
    """Return True if the user message is a harness JSON-parsing error
    rather than real terminal output.  These messages are injected by the
    Nemotron harness when the agent's response could not be parsed as JSON.
    """
    return any(marker in user_content for marker in _HARNESS_ERROR_MARKERS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deterministic_id(conversation_id: str, suffix: str) -> str:
    seed = f"{conversation_id}:{suffix}"
    return "call_" + hashlib.sha256(seed.encode()).hexdigest()[:24]
