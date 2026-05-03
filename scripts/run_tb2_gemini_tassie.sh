#!/usr/bin/env bash
set -euo pipefail

# Run TassieAgent on Terminal-Bench 2.0 with Gemini models (Daytona backend)
# Resumable: re-running this script resumes the previous job if it exists.
#
# Required env vars:
#   DAYTONA_API_KEY    - Daytona API key (sandbox provider for --env daytona)
#   GEMINI_API_KEY     - Google AI Studio API key (used by litellm for gemini/* models)
#
# Optional env vars:
#   MODEL              - Model name (default: gemini/gemini-3-flash-preview)
#   N_CONCURRENT       - Number of concurrent trials (default: 25)
#   MAX_STEPS          - Max agent steps per trial (default: 50)
#   JOB_NAME           - Job name for resumability (default: tb2_gemini)

MODEL="${MODEL:-gemini/gemini-3-flash-preview}"
N_CONCURRENT="${N_CONCURRENT:-25}"
MAX_STEPS="${MAX_STEPS:-50}"
JOB_NAME="${JOB_NAME:-tb2_gemini}"
JOB_DIR="jobs/${JOB_NAME}"

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
        --agent-import-path TassieAgent:TassieAgent \
        --model "$MODEL" \
        --env daytona \
        --n-concurrent "$N_CONCURRENT" \
        --agent-kwarg "max_steps=$MAX_STEPS" \
        --job-name "$JOB_NAME"
fi
