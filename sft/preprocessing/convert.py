"""Core trace conversion: Terminus-2 -> SWE-agent format.

The public entry point is :func:`convert_trace`, designed to be used with
``datasets.Dataset.map``.  It is pure (no I/O, no global mutation): the
caller passes in a :class:`HarnessSpec` (see :mod:`preprocessing.harness`)
that supplies the system prompt, the instance-template wrapper, and the
``tools`` JSON for each row, so the same converter can emit either the
vanillux (default) or legacy tassie framing.
"""

from __future__ import annotations

import json
from pathlib import Path

from preprocessing.json_extraction import extract_json_from_content
from preprocessing.builders import (
    SUBMIT_COMMAND,
    build_reasoning_content,
    build_submit_messages,
    build_tool_calls,
    build_tool_result,
    is_harness_error,
)
from preprocessing.harness import HarnessSpec, get_harness

_CONFIG_DIR = Path(__file__).resolve().parent / "config"
_TOOL_SCHEMAS: list[dict] | None = None

TASK_DELIM = "Task Description:\n"
STATE_DELIM = "Current terminal state:\n"

# Per the SFT parquet schema (matches tmax-sft-skill-tax-... rows produced
# by convert_trajectories._normalise_message), every message struct must
# carry all five keys -- HF Datasets infers a single struct type across
# rows from the first row, so a missing key on row N would surface as a
# silent column-drop. We zero-fill optional fields here.
_MESSAGE_KEYS = ("content", "reasoning_content", "role", "tool_call_ids", "tool_calls")


def get_tool_schemas() -> list[dict]:
    global _TOOL_SCHEMAS
    if _TOOL_SCHEMAS is None:
        _TOOL_SCHEMAS = json.loads((_CONFIG_DIR / "tool_schemas.json").read_text())
    return _TOOL_SCHEMAS


def _normalise_message(msg: dict) -> dict:
    """Coerce a partial message dict to the 5-key parquet schema.

    Mirrors ``convert_trajectories._normalise_message`` so SFT rows from
    the Terminus-2 converter and the rl_data-trajectory converter share
    one Arrow schema (critical for the combined HF dataset upload to
    keep a single struct type per config).
    """
    out: dict = {
        "content": msg.get("content", "") or "",
        "reasoning_content": msg.get("reasoning_content", "") or "",
        "role": msg.get("role", ""),
        "tool_call_ids": list(msg.get("tool_call_ids") or []),
        "tool_calls": list(msg.get("tool_calls") or []),
    }
    return out


def convert_trace(
    row: dict,
    *,
    source_label: str = "",
    conversations_column: str = "conversations",
    harness: HarnessSpec | None = None,
) -> dict:
    """Convert a single Terminus-2 trace into SWE-agent format.

    Parameters
    ----------
    row : dict
        A single dataset row containing at least *conversations_column*.
    source_label : str
        Value to stamp in the ``source`` column (e.g.
        ``"nvidia/Nemotron-Terminal-Corpus/skill_based_easy"``).
    conversations_column : str
        Name of the column holding the raw conversation list.
    harness : HarnessSpec | None
        Which harness frames the output row (default: vanillux). Controls
        the system prompt, the user-side instance wrapping, and the
        emitted ``tools`` JSON.

    Returns
    -------
    dict with keys ``messages``, ``tools``, ``source``, ``metadata``,
    ``warnings``, ``_conversion_ok``.
    """
    if harness is None:
        harness = get_harness()
    messages = row.get(conversations_column, [])
    conversation_id = row.get("trial_name", "unknown")
    warnings: list[str] = []

    # ------------------------------------------------------------------
    # Guard: must start with a user message
    # ------------------------------------------------------------------
    if not messages or messages[0].get("role") != "user":
        return _failure(
            source_label, conversation_id, harness,
            "Invalid trace: does not start with user message",
        )

    # ------------------------------------------------------------------
    # 1. Parse message 0 — extract task description, discard terminal state
    # ------------------------------------------------------------------
    content0 = messages[0]["content"]

    if TASK_DELIM in content0:
        remainder = content0[content0.index(TASK_DELIM) + len(TASK_DELIM):]
    else:
        remainder = content0
        warnings.append("TASK_DELIM not found in message 0; using full content as task")

    if STATE_DELIM in remainder:
        task_description = remainder[: remainder.index(STATE_DELIM)].strip()
    else:
        task_description = remainder.strip()
        warnings.append("STATE_DELIM not found in message 0")

    # ------------------------------------------------------------------
    # 2. Emit system + first user message (instance-wrapped per harness)
    # ------------------------------------------------------------------
    converted: list[dict] = [
        _normalise_message({"role": "system", "content": harness.system_prompt}),
        _normalise_message({
            "role": "user",
            "content": harness.render_instance(task_description),
        }),
    ]

    # ------------------------------------------------------------------
    # 3. Walk (assistant, user) pairs
    # ------------------------------------------------------------------
    i = 1
    turn_index = 0
    strategy_counts: dict[str, int] = {
        "0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0,
    }
    json_failed = False
    has_task_complete = False
    has_ctrl_c = False
    pending_reasoning = ""

    while i < len(messages):
        msg = messages[i]

        if msg.get("role") != "assistant":
            warnings.append(f"Expected assistant at index {i}, got {msg.get('role')}")
            i += 1
            continue

        # --- Parse assistant JSON ---
        parsed, prose, strategy = extract_json_from_content(msg["content"])
        strategy_counts[str(strategy)] += 1

        if parsed is None:
            json_failed = True
            warnings.append(f"JSON extraction failed at index {i}")
            converted.append(_normalise_message({
                "role": "assistant", "content": msg["content"],
            }))
            i += 1
            turn_index += 1
            continue

        reasoning = build_reasoning_content(parsed, prose)
        if prose:
            warnings.append(f"Prose outside JSON at index {i}")

        tool_calls = build_tool_calls(parsed, conversation_id, turn_index)

        next_is_user = (
            (i + 1) < len(messages) and messages[i + 1].get("role") == "user"
        )

        # Track C-c usage and detect premature submit echoes
        if tool_calls:
            cmd = tool_calls[0]["function"]["arguments"].get("command", "")
            if "C-c" in cmd:
                has_ctrl_c = True

            # The harness sometimes re-prompts after a submit attempt
            # ("Are you sure?"), so the model echoes the submit command
            # with task_complete still false.  Skip the turn entirely
            # and buffer its reasoning for the real submit later.
            if cmd.strip() == SUBMIT_COMMAND and not parsed.get("task_complete", False):
                if reasoning:
                    pending_reasoning = (
                        (pending_reasoning + "\n\n" + reasoning).strip()
                        if pending_reasoning else reasoning
                    )
                warnings.append(f"Skipped premature submit echo at index {i}")
                i += 2 if next_is_user else 1
                turn_index += 1
                continue

        # --- Handle tool result (check for harness errors) ---
        if tool_calls and next_is_user:
            raw_tool_content = messages[i + 1]["content"]

            if is_harness_error(raw_tool_content):
                # Drop the entire turn — the harness rejected the JSON and
                # likely did not execute the command.  Skip both assistant
                # and user messages.
                warnings.append(f"Harness error at index {i+1}, dropping turn")
                i += 2
                turn_index += 1
                pending_reasoning = ""
                continue

            tool_result = build_tool_result(raw_tool_content, tool_calls[0]["id"])
            i += 2
        elif tool_calls:
            warnings.append(
                f"Assistant at index {i} has commands but no following user message"
            )
            tool_result = None
            i += 1
        else:
            tool_result = None
            i += 1

        # --- Submit (truncate at first submit) ---
        # Check task_complete BEFORE the reasoning-only buffering path,
        # because Nemotron traces commonly submit with commands: [] which
        # produces empty tool_calls.
        if parsed.get("task_complete", False):
            has_task_complete = True
            if pending_reasoning:
                reasoning = (pending_reasoning + "\n\n" + reasoning).strip()
                pending_reasoning = ""
            submit_msgs = build_submit_messages(
                parsed, conversation_id, turn_index, reasoning,
            )
            converted.extend(_normalise_message(m) for m in submit_msgs)
            turn_index += 1
            break

        # --- Reasoning-only assistant: buffer instead of emitting ---
        if not tool_calls:
            pending_reasoning = (
                (pending_reasoning + "\n\n" + reasoning).strip()
                if pending_reasoning else reasoning
            )
            turn_index += 1
            continue

        # --- Emit assistant message (flush any buffered reasoning) ---
        if pending_reasoning:
            reasoning = (pending_reasoning + "\n\n" + reasoning).strip()
            pending_reasoning = ""

        assistant_msg: dict = {"role": "assistant", "content": ""}
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        converted.append(_normalise_message(assistant_msg))

        if tool_result is not None:
            converted.append(_normalise_message(tool_result))

        turn_index += 1

    # ------------------------------------------------------------------
    # 4. Build metadata
    # ------------------------------------------------------------------
    metadata = {
        "source_model": row.get("model", ""),
        "task": row.get("task", ""),
        "episode": row.get("episode", ""),
        "run_id": row.get("run_id", ""),
        "trial_name": conversation_id,
        "date": row.get("date", ""),
        "enable_thinking": bool(row.get("enable_thinking", False)),
        "num_turns": turn_index,
        "num_warnings": len(warnings),
        "json_strategy_counts": strategy_counts,
        "json_extraction_failed": json_failed,
        "has_task_complete": has_task_complete,
        "has_ctrl_c": has_ctrl_c,
    }

    return {
        "messages": converted,
        "tools": harness.tools_json,
        "source": source_label,
        "metadata": metadata,
        "warnings": warnings,
        "_conversion_ok": True,
    }


def _failure(
    source_label: str,
    conversation_id: str,
    harness: HarnessSpec,
    reason: str,
) -> dict:
    return {
        "messages": [],
        "tools": harness.tools_json,
        "source": source_label,
        "metadata": {"trial_name": conversation_id},
        "warnings": [reason],
        "_conversion_ok": False,
    }
