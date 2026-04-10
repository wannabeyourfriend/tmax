"""SERA (SWE-agent format) trace conversion.

Converts traces from ``allenai/Sera-4.6-Lite-47000`` into our unified
bash-only SWE-agent format.  The SERA dataset is *already* in SWE-agent
format, so this converter performs normalisation rather than structural
transformation:

* Parse ``messages`` from a JSON string.
* Replace the system prompt.
* Flatten structured content (list-of-dicts → plain string).
* Convert ``str_replace_editor`` tool calls to equivalent ``bash`` commands.
* Convert ``submit`` tool calls to our ``echo COMPLETE_TASK_AND_SUBMIT…``
  convention.
* Map the ``thought`` field to ``reasoning_content``.
* Regenerate deterministic tool-call IDs.
* Strip SERA-specific extra fields (``agent``, ``message_type``, ``action``).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from preprocessing.builders import SUBMIT_COMMAND

_CONFIG_DIR = Path(__file__).resolve().parent / "config"
_SYSTEM_PROMPT: str | None = None


def _get_system_prompt() -> str:
    global _SYSTEM_PROMPT
    if _SYSTEM_PROMPT is None:
        _SYSTEM_PROMPT = (_CONFIG_DIR / "system_prompt.txt").read_text().strip()
    return _SYSTEM_PROMPT


def _deterministic_id(conversation_id: str, suffix: str) -> str:
    seed = f"{conversation_id}:{suffix}"
    return "call_" + hashlib.sha256(seed.encode()).hexdigest()[:24]


# ======================================================================
# Content helpers
# ======================================================================

def _flatten_content(content) -> str:
    """Convert SERA's structured content to a plain string.

    SERA encodes user and tool content as ``[{"type": "text", "text": "…"}]``
    while system/assistant content is a plain string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content) if content else ""


_THINK_TAGS = re.compile(r"<think>\s*|\s*</think>")
_OBSERVATION_PREFIX = re.compile(r"^OBSERVATION:\n?")


def _clean_tool_content(content) -> str:
    text = _flatten_content(content)
    text = _OBSERVATION_PREFIX.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ======================================================================
# str_replace_editor → bash conversion
# ======================================================================

def _str_replace_editor_to_bash(args: dict) -> str:
    """Convert a ``str_replace_editor`` tool-call to an equivalent bash command."""
    command = args.get("command", "")
    path = args.get("path", "")

    if command == "view":
        view_range = args.get("view_range")
        if view_range and len(view_range) == 2:
            start, end = view_range
            return f"sed -n '{start},{end}p' {path} | cat -n"
        last_segment = path.rstrip("/").rsplit("/", 1)[-1] if path else ""
        if not last_segment or "." not in last_segment:
            return f"find {path} -maxdepth 2 -not -path '*/\\.*'"
        return f"cat -n {path}"

    if command == "create":
        file_text = args.get("file_text", "")
        return f"cat > {path} << 'ENDOFFILE'\n{file_text}\nENDOFFILE"

    if command == "str_replace":
        old_str = args.get("old_str", "")
        new_str = args.get("new_str", "")
        return _build_python_replace(path, old_str, new_str)

    if command == "insert":
        insert_line = args.get("insert_line", 0)
        new_str = args.get("new_str", "")
        return _build_python_insert(path, insert_line, new_str)

    return f"echo 'unknown str_replace_editor command: {command}'"


def _build_python_replace(path: str, old_str: str, new_str: str) -> str:
    return (
        f"python3 << 'ENDOFSCRIPT'\n"
        f"import pathlib\n"
        f"p = pathlib.Path({path!r})\n"
        f"old = {old_str!r}\n"
        f"new = {new_str!r}\n"
        f"content = p.read_text()\n"
        f"assert old in content, 'old_str not found in file'\n"
        f"p.write_text(content.replace(old, new, 1))\n"
        f"print('The file has been edited.')\n"
        f"ENDOFSCRIPT"
    )


def _build_python_insert(path: str, insert_line: int, new_str: str) -> str:
    return (
        f"python3 << 'ENDOFSCRIPT'\n"
        f"import pathlib\n"
        f"p = pathlib.Path({path!r})\n"
        f"lines = p.read_text().splitlines(True)\n"
        f"lines.insert({insert_line}, {new_str!r} + '\\n')\n"
        f"p.write_text(''.join(lines))\n"
        f"print('The file has been edited.')\n"
        f"ENDOFSCRIPT"
    )


# ======================================================================
# Main converter
# ======================================================================

def convert_sera_trace(
    row: dict,
    *,
    source_label: str = "",
    messages_column: str = "messages",
) -> dict:
    """Convert a single SERA trace into our bash-only SWE-agent format.

    Parameters
    ----------
    row : dict
        A single dataset row.  The *messages_column* is expected to be a
        JSON-encoded string (per SERA dataset convention).
    source_label : str
        Value for the ``source`` output column.
    messages_column : str
        Column holding the raw conversation (default ``"messages"``).

    Returns
    -------
    dict with keys ``messages``, ``source``, ``metadata``, ``warnings``,
    ``_conversion_ok`` — same shape as :func:`convert.convert_trace`.
    """
    conversation_id = row.get("instance_id", "unknown")
    warnings: list[str] = []

    # ------------------------------------------------------------------
    # 0. Parse JSON string
    # ------------------------------------------------------------------
    raw = row.get(messages_column, "")
    if isinstance(raw, str):
        try:
            messages = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            return _failure(source_label, conversation_id,
                            f"JSON parse failed: {exc}")
    elif isinstance(raw, list):
        messages = raw
    else:
        return _failure(source_label, conversation_id,
                        "messages column is neither str nor list")

    if not messages:
        return _failure(source_label, conversation_id, "Empty messages list")

    # ------------------------------------------------------------------
    # 1. System prompt (replace SERA's with ours)
    # ------------------------------------------------------------------
    converted: list[dict] = [
        {"role": "system", "content": _get_system_prompt()},
    ]

    i = 0
    if messages[0].get("role") == "system":
        i = 1

    # ------------------------------------------------------------------
    # 2. First user message
    # ------------------------------------------------------------------
    if i >= len(messages) or messages[i].get("role") != "user":
        return _failure(source_label, conversation_id,
                        "No user message found after system prompt")

    user_content = _flatten_content(messages[i].get("content", ""))
    converted.append({"role": "user", "content": user_content})
    i += 1

    # ------------------------------------------------------------------
    # 3. Walk remaining messages
    # ------------------------------------------------------------------
    turn_index = 0
    has_task_complete = False
    has_ctrl_c = False
    str_replace_editor_count = 0
    id_remap: dict[str, str] = {}

    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        # ---------- assistant ----------
        if role == "assistant":
            thought = _THINK_TAGS.sub("", msg.get("thought") or "").strip()
            tool_calls_raw = msg.get("tool_calls") or []

            if not tool_calls_raw:
                warnings.append(
                    f"Assistant at index {i} has no tool_calls, skipping")
                i += 1
                continue

            new_tool_calls: list[dict] = []
            is_submit = False

            for tc in tool_calls_raw:
                fn = tc.get("function", {})
                fn_name = fn.get("name", "")

                args_raw = fn.get("arguments", "{}")
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw)
                    except json.JSONDecodeError:
                        args = {}
                        warnings.append(
                            f"Failed to parse tool_call arguments at "
                            f"index {i}")
                else:
                    args = args_raw if isinstance(args_raw, dict) else {}

                # --- submit ---
                if fn_name == "submit":
                    is_submit = True
                    has_task_complete = True
                    new_id = _deterministic_id(
                        conversation_id, f"submit{turn_index}")
                    old_id = tc.get("id", "")
                    if old_id:
                        id_remap[old_id] = new_id
                    new_tool_calls = [{
                        "function": {
                            "name": "bash",
                            "arguments": {"command": SUBMIT_COMMAND},
                        },
                        "id": new_id,
                        "type": "function",
                    }]
                    break

                # --- str_replace_editor → bash ---
                if fn_name == "str_replace_editor":
                    command_str = _str_replace_editor_to_bash(args)
                    str_replace_editor_count += 1
                elif fn_name == "bash":
                    command_str = args.get("command", "")
                else:
                    command_str = args.get("command", str(args))
                    warnings.append(
                        f"Unknown tool '{fn_name}' at index {i}")

                if "C-c" in command_str:
                    has_ctrl_c = True

                new_id = _deterministic_id(
                    conversation_id, f"turn{turn_index}")
                old_id = tc.get("id", "")
                if old_id:
                    id_remap[old_id] = new_id

                new_tool_calls.append({
                    "function": {
                        "name": "bash",
                        "arguments": {"command": command_str},
                    },
                    "id": new_id,
                    "type": "function",
                })

            assistant_msg: dict = {"role": "assistant", "content": ""}
            if thought:
                assistant_msg["reasoning_content"] = thought
            if new_tool_calls:
                assistant_msg["tool_calls"] = new_tool_calls
            converted.append(assistant_msg)

            turn_index += 1

            if is_submit:
                break

            i += 1

        # ---------- tool ----------
        elif role == "tool":
            tool_content = _clean_tool_content(msg.get("content", ""))

            old_ids = msg.get("tool_call_ids") or []
            new_ids = [id_remap.get(oid, oid) for oid in old_ids]

            converted.append({
                "role": "tool",
                "content": tool_content,
                "tool_call_ids": new_ids,
            })
            i += 1

        else:
            warnings.append(f"Unexpected role '{role}' at index {i}")
            i += 1

    # ------------------------------------------------------------------
    # 4. Metadata
    # ------------------------------------------------------------------
    metadata = {
        "instance_id": conversation_id,
        "func_name": row.get("func_name", ""),
        "func_path": row.get("func_path", ""),
        "sera_source": row.get("source", ""),
        "docker_image": row.get("docker_image", ""),
        "num_turns": turn_index,
        "num_warnings": len(warnings),
        "json_extraction_failed": False,
        "has_task_complete": has_task_complete,
        "has_ctrl_c": has_ctrl_c,
        "str_replace_editor_count": str_replace_editor_count,
    }

    return {
        "messages": converted,
        "source": source_label,
        "metadata": metadata,
        "warnings": warnings,
        "_conversion_ok": True,
    }


def _failure(source_label: str, conversation_id: str, reason: str) -> dict:
    return {
        "messages": [],
        "source": source_label,
        "metadata": {"instance_id": conversation_id},
        "warnings": [reason],
        "_conversion_ok": False,
    }
