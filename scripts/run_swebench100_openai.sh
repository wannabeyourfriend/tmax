#!/usr/bin/env bash
set -euo pipefail

# Run TassieAgent on 100 deterministic SWE-Bench Verified tasks with OpenAI models
# The 100 tasks are pre-selected in scripts/swebench100_tasks.txt (seed=42).
#
# Required env vars:
#   OPENAI_API_KEY     - OpenAI API key
#
# Optional env vars:
#   MODEL              - Model name (default: openai/gpt-4o)
#   N_CONCURRENT       - Number of concurrent trials (default: 25)
#   MAX_STEPS          - Max agent steps per trial (default: 100)
#   JOB_NAME           - Job name for resumability (default: swebench100_openai)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL="${MODEL:-openai/gpt-4o}"
N_CONCURRENT="${N_CONCURRENT:-25}"
MAX_STEPS="${MAX_STEPS:-100}"
JOB_NAME="${JOB_NAME:-swebench100_openai}"
JOB_DIR="jobs/${JOB_NAME}"

TASK_ARGS=()
while IFS= read -r task; do
    TASK_ARGS+=("--task-name" "$task")
done < "$SCRIPT_DIR/swebench100_tasks.txt"

if [ -d "$JOB_DIR" ]; then
    echo "Resuming job from $JOB_DIR"
    uv run harbor jobs resume \
        --job-path "$JOB_DIR" \
        --filter-error-type DaytonaError
else
    uv run harbor run \
        --dataset swebench-verified@1.0 \
        --agent-import-path TassieAgent:TassieAgent \
        --model "$MODEL" \
        --env daytona \
        --n-concurrent "$N_CONCURRENT" \
        --agent-kwarg "max_steps=$MAX_STEPS" \
        --job-name "$JOB_NAME" \
        -k 5 \
        "${TASK_ARGS[@]}"
fi
