"""VanilluxAgent — Harbor SWE-agent variant with mild tweaks on upstream defaults.

Inherits from harbor's installed ``SweAgent`` adapter (which itself runs the
upstream SWE-agent (Yang et al., 2024) inside the sandbox) and only changes the
CLI flags passed to ``sweagent run``. The SWE-agent config used is the upstream
``config/default.yaml`` — i.e. exactly the bundles ``tools/registry +
tools/edit_anthropic + tools/review_on_submit_m`` plus ``enable_bash_tool: true``,
which gives the agent the four tools the paper's vanilla setup specifies:

    "we only provide the agent with the ability to run a view tool, edit
     tool, submission tool, and bash commands"

The default config does NOT add a ``last_n_observations`` history processor, so
context history is sent in full on every step. The only ``history_processors``
entry in the default is ``cache_control`` (last_n_messages: 2), which is just an
Anthropic prompt-caching annotation — it doesn't drop or shorten any messages.

What we override:

* ``agent.model.per_instance_cost_limit`` (default $3.00) — bumped to $10 so
  individual runs aren't cut short by the budget cap, but a runaway agent still
  has a hard ceiling.

* ``agent.model.total_cost_limit`` (default $0 = unlimited) — left at 0
  explicitly. SWE-agent treats 0 as "no limit"
  (``if 0 < total_cost_limit < instance_cost`` in ``sweagent/agent/models.py``).

* ``agent.model.per_instance_call_limit`` (default 0 = unlimited) — set via
  the ``VANILLUX_CALL_LIMIT`` env var, defaulting to 50 to mirror our
  TassieAgent ``max_steps=50`` runs. On TB2 we observed that 56/89 trials hit
  the 50-call cap with ~52% of those still managing to solve, so bumping this
  to e.g. 100 is a useful experiment (the run script encodes it into the job
  name so different caps land in different ``jobs/`` dirs).

* ``agent.history_processors`` (default ``[{type: cache_control,
  last_n_messages: 2}]``) — set to ``[]``. The cache_control processor stamps
  ``cache_control: {type: ephemeral}`` markers on the last N messages for
  Anthropic prompt-caching. With Gemini, litellm (≥1.x) sees those markers and
  routes the request through its Vertex AI context-caching path
  (``check_and_create_cache``), which then crashes inside its tool-call /
  tool-response pairing logic with ``Exception: Missing corresponding tool call
  for tool response message`` — every single LM call fails and the agent makes
  no progress (we observed this on Gemini 3 Flash Preview, all 89 TB2 trials).
  The default config has no other processors, so ``[]`` is safe; for non-Gemini
  models you may want to revert this to keep Anthropic prompt caching working.

  We can't pass this as ``--agent.history_processors=[]`` on the CLI: SWE-agent's
  ``BasicCLI`` uses argparse's ``--key=value`` parsing (``sweagent/run/common.py``
  ``_parse_args_to_nested_dict``), which treats the value as a literal string,
  so pydantic then sees ``['[]']`` (a one-element list whose element is the
  string ``"[]"``) and rejects it. Instead we write a tiny YAML override file
  inside the sandbox and pass it as a *second* ``--config``. SWE-agent merges
  configs via ``merge_nested_dicts`` (``sweagent/utils/serialization.py``),
  which is recursive on dicts but replaces list values wholesale, so our
  ``history_processors: []`` cleanly wipes out the default's processor list.

Everything else (parser, prompts, tool bundles, ``max_observation_length``,
``total_execution_timeout``, the upstream SWE-agent version pinned by the
harbor install template, etc.) is left at upstream defaults.

We also patch around two bugs in harbor's installed-SWE-agent run prelude:

1. The install template writes ``/etc/profile.d/testbed-conda.sh`` with a bare
   ``[ -z "$CONDA_DEFAULT_ENV" ]`` test, then the run command sources that file
   under ``set -euo pipefail``. On non-SWE-bench sandboxes (e.g. Daytona TB2)
   ``CONDA_DEFAULT_ENV`` is unset, so ``set -u`` aborts the script before
   ``sweagent run`` is ever invoked. We define ``CONDA_DEFAULT_ENV=""`` just
   before sourcing so the existence test passes safely; the inner activation
   block then correctly skips itself because ``/opt/miniconda3/envs/testbed``
   doesn't exist on those sandboxes.

2. The fallback-repo argument ``else echo '--env.repo.path=$(pwd)'`` is
   single-quoted, so ``$(pwd)`` is passed as a literal string and SWE-agent's
   ``LocalRepoConfig`` then crashes with ``NoSuchPathError: /app/$(pwd)``. Even
   with the quoting fixed, ``LocalRepoConfig`` requires the path to be a valid
   git repo (it does ``GitRepo(path, search_parent_directories=True)``), which
   TB2's ``/app`` working directory is not. We replace the whole conditional
   with a static ``--env.repo.type=preexisting --env.repo.repo_name=app
   --env.repo.reset=false``: ``PreExistingRepoConfig.copy()`` is a no-op, and
   with ``reset=False`` no git commands are run, so SWE-agent simply ``cd``s
   into ``/app`` and starts.

   Note we use ``repo_name=app`` (no leading slash). SWE-agent's
   ``_reset_repository`` builds ``f"cd /{repo_name}"``, so a value of ``/app``
   would produce ``cd //app``; some Daytona images' bash doesn't collapse the
   double slash and the command fails. ``repo_name=app`` correctly resolves to
   ``cd /app``.

   Most TB2 tasks land the agent's working directory at ``/app``, but a small
   number of tasks (e.g. ``prove-plus-comm``) keep their files at a different
   pwd. For those, the static ``cd /app`` from ``_reset_repository`` would
   fail with ``bash: cd: /app: No such file or directory`` and SWE-agent
   would never start. To handle this we add a tiny prelude line that
   symlinks ``/app -> $(pwd)`` whenever ``/app`` doesn't already exist (and
   pwd isn't ``/`` — guarding against the pathological case of the sandbox
   landing at the root). When ``/app`` already exists (the common case) the
   line is a no-op.
"""

from __future__ import annotations

import os

from harbor.agents.installed.base import ExecInput
from harbor.agents.installed.swe_agent import SweAgent


class VanilluxAgent(SweAgent):
    """SWE-agent with a higher per-instance budget and a configurable step cap."""

    CALL_LIMIT: int = int(os.environ.get("VANILLUX_CALL_LIMIT", "50"))

    EXTRA_FLAGS: list[str] = [
        "--agent.model.per_instance_cost_limit=10",
        "--agent.model.total_cost_limit=0",
        f"--agent.model.per_instance_call_limit={CALL_LIMIT}",
    ]

    _OVERRIDES_PATH: str = "/tmp/vanillux_overrides.yaml"
    _ENSURE_APP_DIR: str = (
        '[ -d /app ] || { _P="$(pwd)"; [ "$_P" != "/" ] && ln -sf "$_P" /app; }\n'
    )
    _WRITE_OVERRIDES: str = (
        f"cat > {_OVERRIDES_PATH} <<'VANILLUX_OVERRIDES_EOF'\n"
        "agent:\n"
        "  history_processors: []\n"
        "VANILLUX_OVERRIDES_EOF\n"
    )
    _DEFAULT_CONFIG_FLAG: str = '--config="/opt/sweagent-configs/default.yaml"'
    _MERGED_CONFIG_FLAGS: str = (
        f'{_DEFAULT_CONFIG_FLAG} --config={_OVERRIDES_PATH}'
    )

    _BROKEN_REPO_CLAUSE: str = (
        "$(if [ -d /testbed ]; then echo "
        "'--env.repo.type=preexisting --env.repo.repo_name=/testbed'; "
        "else echo '--env.repo.path=$(pwd)'; fi)"
    )
    _FIXED_REPO_CLAUSE: str = (
        "--env.repo.type=preexisting "
        "--env.repo.repo_name=app "
        "--env.repo.reset=false"
    )

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        exec_inputs = super().create_run_agent_commands(instruction)
        extra = " ".join(self.EXTRA_FLAGS)
        for ei in exec_inputs:
            ei.command = ei.command.replace(
                "sweagent run",
                self._ENSURE_APP_DIR
                + self._WRITE_OVERRIDES
                + f"sweagent run {extra}",
                1,
            )
            if self._DEFAULT_CONFIG_FLAG not in ei.command:
                raise RuntimeError(
                    "VanilluxAgent: harbor's SweAgent.create_run_agent_commands "
                    "no longer emits the expected --config=\"/opt/sweagent-configs/"
                    "default.yaml\" flag; the VanilluxAgent string-patch needs "
                    "updating."
                )
            ei.command = ei.command.replace(
                self._DEFAULT_CONFIG_FLAG, self._MERGED_CONFIG_FLAGS, 1,
            )
            ei.command = ei.command.replace(
                ". /etc/profile.d/testbed-conda.sh",
                'export CONDA_DEFAULT_ENV="${CONDA_DEFAULT_ENV:-}"\n'
                ". /etc/profile.d/testbed-conda.sh",
                1,
            )
            if self._BROKEN_REPO_CLAUSE not in ei.command:
                raise RuntimeError(
                    "VanilluxAgent: harbor's SweAgent.create_run_agent_commands "
                    "no longer emits the expected $(pwd)-based repo clause; the "
                    "VanilluxAgent string-patch needs updating."
                )
            ei.command = ei.command.replace(
                self._BROKEN_REPO_CLAUSE, self._FIXED_REPO_CLAUSE, 1,
            )
        return exec_inputs
