"""Harness specifications used by the SFT conversion pipeline.

The "harness" controls how a single SFT row is framed for the model:

* what goes in the ``system`` message (a fixed prompt string),
* how the raw task description is wrapped into the first ``user`` message
  (a ``render_instance`` callable), and
* what OpenAI-style tool definitions are attached to the row's ``tools``
  column.

We support two harnesses:

``vanillux`` (the default; current production harness)
    Mirrors ``Vanillux2Agent`` / ``rl_data.generator.vanillux_solver``:
    system prompt and instance template come straight from
    ``rl_data/generator/vanillux_prompts.yaml`` (vendored from
    mini-swe-agent v2). The first user message is rendered through
    ``render_instance`` which substitutes ``{{task}}`` into the
    "Recommended Workflow" template so the SFT row matches what the model
    sees at solve time exactly.

``tassie`` (legacy; opt-in via ``--harness tassie``)
    Reproduces the ``TassieAgent`` / ``tmax-sft-full-20260409`` framing:
    system prompt is the persistent-bash prompt stored in
    ``sft/preprocessing/config/system_prompt.txt`` (still read at
    solve time by ``rl_data.generator.sample_solutions``), and the first
    user message is the bare task description with no instance wrapping.

Both harnesses share the same single-``bash`` tool spec loaded from
``sft/preprocessing/config/tool_schemas.json`` -- only the prompt text
differs. That tool spec is identical to the one
``convert_trajectories._load_default_tools_json`` writes into the
``tools`` column of skill_tax pass-through rows, so all SFT rows in the
combined dataset carry the same tools JSON regardless of source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

_CONFIG_DIR = Path(__file__).resolve().parent / "config"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_VANILLUX_PROMPTS_PATH = _REPO_ROOT / "rl_data" / "generator" / "vanillux_prompts.yaml"


@dataclass(frozen=True)
class HarnessSpec:
    """A frozen description of an SFT harness.

    Attributes
    ----------
    name : str
        Stable identifier for this harness (``"vanillux"`` or ``"tassie"``).
        Used by ``run_conversion.sh`` and ``pipeline.py``'s ``--harness``
        flag.
    system_prompt : str
        The exact string emitted as the ``content`` of every row's
        ``system`` message.
    tools_json : str
        Compact JSON string (a list of OpenAI tool definitions) written
        into every row's ``tools`` column. Same shape as
        ``convert_trajectories._DEFAULT_TOOLS_JSON``.
    render_instance : Callable[[str], str]
        Transform applied to the raw task description before it becomes
        the first ``user`` message. Vanillux substitutes ``{{task}}`` into
        the instance template; Tassie returns the task unchanged.
    """

    name: str
    system_prompt: str
    tools_json: str
    render_instance: Callable[[str], str]


# ---------------------------------------------------------------------------
# Tools JSON (shared by both harnesses)
# ---------------------------------------------------------------------------


def _load_tools_json() -> str:
    """Load tool_schemas.json and return a compact JSON string.

    Same source-of-truth file as ``rl_data.generator.sample_solutions``
    (legacy bash harness) and ``rl_data.generator.vanillux_solver``
    (vanillux harness), so the ``tools`` column always matches what the
    model was prompted with at solve time.
    """
    raw = json.loads((_CONFIG_DIR / "tool_schemas.json").read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError(
            f"Expected a JSON array in tool_schemas.json, got {type(raw).__name__}"
        )
    return json.dumps(raw, ensure_ascii=False)


_TOOLS_JSON = _load_tools_json()


# ---------------------------------------------------------------------------
# Vanillux (default)
# ---------------------------------------------------------------------------


def _load_vanillux_prompts() -> dict:
    with _VANILLUX_PROMPTS_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_VANILLUX_PROMPTS = _load_vanillux_prompts()
_VANILLUX_SYSTEM_TEMPLATE: str = _VANILLUX_PROMPTS["system_template"]
_VANILLUX_INSTANCE_TEMPLATE: str = _VANILLUX_PROMPTS["instance_template"]


def _render_vanillux_instance(task: str) -> str:
    """Substitute ``{{task}}`` into vanillux's instance_template.

    Mirrors ``rl_data.generator.vanillux_solver._render_instance`` --
    literal string replace, no Jinja runtime, so SFT rows are bit-for-bit
    identical to what ``Vanillux2Agent`` sends at solve time.
    """
    return _VANILLUX_INSTANCE_TEMPLATE.replace("{{task}}", task)


VANILLUX = HarnessSpec(
    name="vanillux",
    system_prompt=_VANILLUX_SYSTEM_TEMPLATE,
    tools_json=_TOOLS_JSON,
    render_instance=_render_vanillux_instance,
)


# ---------------------------------------------------------------------------
# Tassie (legacy)
# ---------------------------------------------------------------------------


_TASSIE_SYSTEM_PROMPT: str = (_CONFIG_DIR / "system_prompt.txt").read_text(
    encoding="utf-8"
).strip()

# The legacy tmax-sft-full-20260409 build kept ``"Task Description:\n"``
# as a literal prefix in the first user message (a quirk of how
# ``convert.py`` used to slice ``content0[content0.index(TASK_DELIM):]``).
# We re-add it here so ``--harness tassie`` reproduces those rows
# byte-for-byte even though ``convert.py`` now extracts the bare task.
_TASSIE_TASK_PREFIX = "Task Description:\n"


def _render_tassie_instance(task: str) -> str:
    """Legacy framing: prepend the ``Task Description:`` header, no wrapping."""
    return _TASSIE_TASK_PREFIX + task


TASSIE = HarnessSpec(
    name="tassie",
    system_prompt=_TASSIE_SYSTEM_PROMPT,
    tools_json=_TOOLS_JSON,
    render_instance=_render_tassie_instance,
)


# ---------------------------------------------------------------------------
# Registry / factory
# ---------------------------------------------------------------------------


HARNESSES: dict[str, HarnessSpec] = {
    "vanillux": VANILLUX,
    "tassie": TASSIE,
}

DEFAULT_HARNESS_NAME = "vanillux"


def get_harness(name: str | None = None) -> HarnessSpec:
    """Look up a harness by name; defaults to vanillux on ``None``.

    Raises ``ValueError`` (not KeyError) on unknown names so CLI error
    messages stay user-friendly.
    """
    if name is None:
        name = DEFAULT_HARNESS_NAME
    try:
        return HARNESSES[name]
    except KeyError:
        valid = ", ".join(sorted(HARNESSES))
        raise ValueError(
            f"Unknown harness {name!r}. Available: {valid}."
        ) from None
