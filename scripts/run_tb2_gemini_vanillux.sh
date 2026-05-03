#!/usr/bin/env bash
set -euo pipefail

# Run VanilluxAgent (a thin wrapper around upstream SWE-agent, Yang et al. 2024)
# on Terminal-Bench 2.0 with Gemini models (Daytona backend), via harbor's
# installed-agent adapter. Resumable.
#
# Mostly the upstream SWE-agent default config (config/default.yaml):
#   * Tools = bash + view/create/str_replace/insert/undo_edit (tools/edit_anthropic)
#       + submit (tools/review_on_submit_m). Matches the paper's vanilla setup:
#     "we only provide the agent with the ability to run a view tool, edit
#      tool, submission tool, and bash commands"
#   * No context-history truncation: default has only a `cache_control`
#     processor (Anthropic prompt-caching marker, not truncation), no
#     `last_n_observations`. We disable cache_control entirely (see below).
#   * Tool outputs >100k chars are truncated by SWE-agent (upstream default,
#     not overridden here).
#
# Tweaks applied by VanilluxAgent:
#   * agent.model.per_instance_cost_limit = 10  (default $3, raised so runs
#     aren't cut short by the budget cap).
#   * agent.model.total_cost_limit = 0  (no cap across all instances; SWE-agent
#     treats 0 as "no limit").
#   * agent.model.per_instance_call_limit = $CALL_LIMIT  (default 100 here;
#     50 is comparable to MAX_STEPS in the TassieAgent script). On TB2 the
#     50-cap binds for ~63% of trials, so 100 gives more room for harder
#     tasks. The agent reads this from VANILLUX_CALL_LIMIT (set below).
#   * agent.history_processors = []  (disables cache_control). With Gemini,
#     litellm interprets the cache_control markers and routes through Vertex AI
#     context-caching, which crashes on tool-call/tool-response pairing —
#     causing every LM call to fail. Applied via a runtime-written YAML
#     override config (cannot be set via CLI: SWE-agent's BasicCLI parses
#     ``--agent.history_processors=[]`` as the literal string ``"[]"`` and
#     pydantic then rejects it).
#
# Required env vars:
#   DAYTONA_API_KEY    - Daytona API key (sandbox provider for --env daytona)
#   GEMINI_API_KEY     - Google AI Studio API key (used by litellm for gemini/* models)
#
# Optional env vars:
#   MODEL              - Model name (default: gemini/gemini-3-flash-preview)
#   N_CONCURRENT       - Number of concurrent trials (default: 25)
#   CALL_LIMIT         - Per-instance LM call cap (default: 100). Encoded into
#                        the default JOB_NAME so different caps land in
#                        separate jobs/ dirs.
#   JOB_NAME           - Job name for resumability
#                        (default: tb2_gemini_vanillux_calls${CALL_LIMIT})

MODEL="${MODEL:-gemini/gemini-3-flash-preview}"
N_CONCURRENT="${N_CONCURRENT:-25}"
CALL_LIMIT="${CALL_LIMIT:-100}"
JOB_NAME="${JOB_NAME:-tb2_gemini_vanillux_calls${CALL_LIMIT}}"
JOB_DIR="jobs/${JOB_NAME}"
# Picked up by VanilluxAgent.CALL_LIMIT at module import time.
export VANILLUX_CALL_LIMIT="$CALL_LIMIT"

# Harbor hardcodes its task cache to ~/.cache/harbor (see harbor.constants).
# On Tillicum the home filesystem has a tight per-user quota, and TB 2.0 LFS
# blobs (e.g. video-processing/*.mp4) blow it almost immediately. Redirect to
# scratch via a symlink the first time we run.
HARBOR_CACHE_TARGET="${HARBOR_CACHE_TARGET:-/gpfs/scrubbed/osey/harbor_cache}"
mkdir -p "$HARBOR_CACHE_TARGET"
if [ ! -L "$HOME/.cache/harbor" ]; then
    mkdir -p "$HOME/.cache"
    if [ -d "$HOME/.cache/harbor" ]; then
        rm -rf "$HOME/.cache/harbor"
    fi
    ln -s "$HARBOR_CACHE_TARGET" "$HOME/.cache/harbor"
fi

# Only resume if harbor actually wrote a config.json. A crashed first run
# (e.g. during task download) can leave an empty $JOB_DIR with just job.log,
# which makes `harbor jobs resume` bail with "Config file not found".
if [ -f "$JOB_DIR/config.json" ]; then
    echo "Resuming job from $JOB_DIR"
    uv run harbor jobs resume \
        --job-path "$JOB_DIR" \
        --filter-error-type DaytonaError
else
    if [ -d "$JOB_DIR" ]; then
        echo "Stale $JOB_DIR (no config.json) — removing and starting fresh"
        rm -rf "$JOB_DIR"
    fi
    uv run harbor run \
        --dataset terminal-bench@2.0 \
        --agent-import-path VanilluxAgent:VanilluxAgent \
        --model "$MODEL" \
        --env daytona \
        --n-concurrent "$N_CONCURRENT" \
        --job-name "$JOB_NAME"
fi
