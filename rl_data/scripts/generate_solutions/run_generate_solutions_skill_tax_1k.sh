#!/bin/bash
#SBATCH --job-name=rl-gen-sol-stx-1k
#SBATCH --output=logs/gen_sol_stx_1k_%j.out
#SBATCH --error=logs/gen_sol_stx_1k_%j.err
#SBATCH --time=24:00:00
#SBATCH --ntasks=1
#SBATCH --gres=gpu:h200:8
#SBATCH --cpus-per-task=64
#SBATCH --mem=960G

# ╔═══════════════════════════════════════════════════════════════════════╗
# ║  Run our solution-generation harness on tasks_skill_tax_20260324_1k   ║
# ║  (1036 tasks, our reference 1k dataset) under a LOCAL Qwen3.x-27B     ║
# ║  served in-job via vLLM.  These trajectories feed the SFT data we     ║
# ║  use to teach Qwen3.x-{4B,9B} the bash-tool-agent loop.               ║
# ║                                                                        ║
# ║  Default model: Qwen/Qwen3.6-27B (override via VLLM_MODEL=...).        ║
# ║  Qwen3.6 shares the qwen3_5 arch tag with Qwen3.5 and so reuses the   ║
# ║  same vLLM serving toggles (qwen3_coder tool-call parser, qwen3       ║
# ║  reasoning parser, --language-model-only); the family detection in   ║
# ║  comparison/_vllm_local.sh handles both transparently.                ║
# ║                                                                        ║
# ║  Defaults are tuned for: ALL 1036 tasks × 8 solutions each, on a      ║
# ║  single 8xH200 allocation, with TP=2 DP=4 for the 27B (54 GB bf16     ║
# ║  params; H200 has 141 GB, so each replica gets ~70 GB to share        ║
# ║  between params + KV across TP=2).                                    ║
# ║                                                                        ║
# ║  Notes:                                                                ║
# ║   * PRE-DOWNLOAD recommended on a login node before the first run     ║
# ║     against a new model:                                              ║
# ║         bash rl_data/scripts/predownload_model.sh Qwen/Qwen3.6-27B    ║
# ║     The compute-node network is much slower than the login node and  ║
# ║     a 56 GB cold pull eats most of an SBATCH wall budget.            ║
# ║   * Skill-tax tasks ship per-task `container.def`s (Bootstrap: docker ║
# ║     From: ubuntu:22.04) -- no shared base SIF to bootstrap, so this   ║
# ║     script omits the base-SIF section that ET / OT / TermiGen need.   ║
# ║     Per-task SIF builds happen lazily inside the harness.             ║
# ║   * Anonymous Docker Hub pulls are rate-limited (100 / 6 h / IP) and   ║
# ║     1k concurrent SIF builds easily blow that, so APPTAINER_DOCKER_*   ║
# ║     creds are required (same pattern as ET).                          ║
# ║   * The trajectory output (per-task hosted_vllm_Qwen_Qwen3.6-27B_     ║
# ║     summary.json) is the input to                                     ║
# ║     `sft/preprocessing/convert_trajectories.py` -- run that next.    ║
# ╚═══════════════════════════════════════════════════════════════════════╝

set -euo pipefail

# ---- Parameters (edit here) ----
TASKS_DIR="rl_data/output/tasks_skill_tax_20260324_1k"

# This script is local-vLLM ONLY.  Default LAUNCH_VLLM=1 so users don't have
# to remember the flag, and default MODEL to the canonical hosted_vllm/...
# id that _vllm_wait_ready_local would otherwise rewrite it to.  Override
# via env to point at a different vLLM model (set both VLLM_MODEL + MODEL).
export LAUNCH_VLLM="${LAUNCH_VLLM:-1}"
export VLLM_MODEL="${VLLM_MODEL:-Qwen/Qwen3.6-27B}"
MODEL="${MODEL:-hosted_vllm/${VLLM_MODEL}}"

# NUM_SOLUTIONS: 8 trajectories per task.  Critical for the SFT-data use case --
# the success-only variant of the SFT data is meaningfully different from the
# all variant only when there are multiple trajectories per task to filter.
NUM_SOLUTIONS="${NUM_SOLUTIONS:-8}"
MAX_ACTIONS=16
# MAX_TOKENS is auto-capped to ~vllm_max_len/4 by _vllm_wait_ready_local
# (the cap defaults to 10240 for the Qwen3.5+ family's 40960-token window
# we set via VLLM_MAX_LEN; Qwen3.6's native 262144 is bigger but we don't
# need it for agent loops and longer windows hurt throughput).
MAX_TOKENS="${MAX_TOKENS:-65536}"
NUM_TASKS=999999
START_AT=0
SOLUTION_TEMPERATURE=0.7
COMMAND_TIMEOUT=60
SHELL_INIT_TIMEOUT=240
SHELL_INIT_ATTEMPTS=3
BUILD_WORKERS=4              # mostly a no-op with BASE_SIFS_DIR (no per-task SIFs to build)
BUILD_RETRIES=3
# CRITICAL: skill-tax's per-task `container.def`s would each fresh-install
# python3+pip+pytest+gcc+gawk against archive.ubuntu.com if built from scratch,
# which thunder-herds under WORKERS=12 concurrent builds and trips the 300s
# build timeout on ~40% of tasks.  Instead we use the 9 pre-built domain base
# SIFs in rl_data/containers/ + the per-task setup.sh deltas (already extracted
# next to each task.json).  This is the SAME mode the original gemini 10k run
# used; see rl_data/scripts/generate_solutions/run_generate_solutions_10k.sh
# for the canonical pattern.
BASE_SIFS_DIR="${BASE_SIFS_DIR:-rl_data/containers}"
FORCE_RERUN=0
LOG_COMMANDS=0
DISABLE_TERMINAL_LOG=0

# SAMPLE_SIZE=0 -> process all 1036 tasks (the user's stated intent: "run the
# entire 1k data").  Override via env if you want a quick smoke test.
SAMPLE_SIZE="${SAMPLE_SIZE:-0}"
SAMPLE_SEED="${SAMPLE_SEED:-0}"

# WORKERS = concurrent TASKS at once.  NUM_POOL_WORKERS = concurrent solutions
# within a single task.  Both env-overridable.
WORKERS="${WORKERS:-12}"
NUM_POOL_WORKERS="${NUM_POOL_WORKERS:-16}"

# ---- vLLM tensor-parallel sizing for the 27B ----
# Qwen3.5-27B / Qwen3.6-27B at bf16 are ~54-56 GB of params.  TP=2 DP=4 on
# 8xH200 gives 4 replicas of ~30 GB params each (split across 2 GPUs),
# leaving ~110 GB per GPU for KV cache + activations -- plenty of headroom
# for our concurrency (WORKERS=12 * NUM_SOLUTIONS=8 = 96 in-flight requests
# at peak).  DP is auto-derived as visible_gpus / TP by _vllm_local.sh, so
# we only need to set TP here.
export VLLM_TP="${VLLM_TP:-2}"
# --------------------------------

_RUN_TS=$(date -u +%Y%m%d_%H%M%S)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
COMPARISON_DIR="$PROJECT_ROOT/rl_data/scripts/comparison"

cd "$PROJECT_ROOT"
mkdir -p logs

# In-job vLLM bring-up.  No-op unless LAUNCH_VLLM=1.  Helper auto-detects
# the Qwen3.5+ arch family (matches `qwen3.[5-9]` case-insensitive, so it
# picks up Qwen3.5 / Qwen3.6 / future Qwen3.x) and switches to the
# qwen3_coder tool-call parser + qwen3 reasoning parser + --language-model-only
# flag.  Sourced from comparison/ since that's where the canonical helper
# lives (one source of truth for all solve scripts).
# shellcheck source=../comparison/_vllm_local.sh
source "$COMPARISON_DIR/_vllm_local.sh"
_vllm_start_local

# Apptainer Docker Hub creds: required because skill-tax per-task defs use
# Bootstrap: docker From: ubuntu:22.04 directly, and 1k concurrent anonymous
# pulls would exceed the 100 / 6 h / IP rate limit.  Same pattern as ET.
export APPTAINER_DOCKER_USERNAME="${APPTAINER_DOCKER_USERNAME:?Set APPTAINER_DOCKER_USERNAME before running}"
export APPTAINER_DOCKER_PASSWORD="${APPTAINER_DOCKER_PASSWORD:?Set APPTAINER_DOCKER_PASSWORD before running}"

# Local-model support via litellm env passthrough.  Harmless if unset.
export HOSTED_VLLM_API_BASE="${HOSTED_VLLM_API_BASE:-}"
export OLLAMA_API_BASE="${OLLAMA_API_BASE:-}"
export OPENAI_API_BASE="${OPENAI_API_BASE:-}"
if [[ -n "${HOSTED_VLLM_API_BASE:-}${OLLAMA_API_BASE:-}${OPENAI_API_BASE:-}" && -z "${OPENAI_API_KEY:-}" ]]; then
  export OPENAI_API_KEY="EMPTY"
fi

export APPTAINER_CACHEDIR="/gpfs/projects/h2lab/osey/apptainer_cache"
export APPTAINER_TMPDIR="/tmp/apptainer_tmp"
mkdir -p "$APPTAINER_TMPDIR"

# Keep apptainer instance logs off GPFS; heal dangling symlinks left behind
# when /tmp was cleaned between runs.
mkdir -p /tmp/apptainer_instances
if [ ! -L "$HOME/.apptainer/instances" ]; then
  rm -rf "$HOME/.apptainer/instances"
  ln -s /tmp/apptainer_instances "$HOME/.apptainer/instances"
fi

# Block until the in-job vLLM server is ready (no-op when LAUNCH_VLLM!=1).
# Also (re)exports MODEL/HOSTED_VLLM_API_BASE/OPENAI_API_KEY for the solver,
# and auto-caps MAX_TOKENS to fit the 27B's max_model_len (default 40960 ->
# cap at ~10240 leaving 30720 for the prompt + history).
_vllm_wait_ready_local

# Derive model-tagged paths NOW (after the helper may have rewritten MODEL).
_MODEL_TAG=$(echo "$MODEL" | tr '/' '_')
TERMINAL_LOG="${TASKS_DIR}/logs/${_MODEL_TAG}_${_RUN_TS}.log"

EXTRA_ARGS=()
if [[ "${FORCE_RERUN:-0}" == "1" ]]; then
  EXTRA_ARGS+=(--force-rerun)
fi
if [[ "${LOG_COMMANDS:-0}" == "1" ]]; then
  EXTRA_ARGS+=(--log-commands)
fi
if [[ -n "${BASE_SIFS_DIR:-}" ]]; then
  EXTRA_ARGS+=(--base-sifs-dir "$BASE_SIFS_DIR")
fi
if [[ "${SAMPLE_SIZE:-0}" != "0" ]]; then
  EXTRA_ARGS+=(--sample-size "$SAMPLE_SIZE" --sample-seed "$SAMPLE_SEED")
fi
if [[ "${DISABLE_TERMINAL_LOG:-0}" != "1" ]]; then
  TL="${TERMINAL_LOG}"
  if [[ "$TL" != /* ]]; then
    TL="$PROJECT_ROOT/$TL"
  fi
  mkdir -p "$(dirname "$TL")"
  EXTRA_ARGS+=(--terminal-log "$TL")
fi

echo "=== skill_tax 1k SFT-data run: MODEL=${MODEL}, WORKERS=${WORKERS}, NUM_SOLUTIONS=${NUM_SOLUTIONS} ==="
echo "=== Concurrent containers: $(( WORKERS * NUM_SOLUTIONS )) ==="

uv run python -m rl_data.generate_solutions \
    --tasks-dir "$TASKS_DIR" \
    --model "$MODEL" \
    --num-solutions "$NUM_SOLUTIONS" \
    --max-actions "$MAX_ACTIONS" \
    --max-tokens "$MAX_TOKENS" \
    --num-tasks "$NUM_TASKS" \
    --start-at "$START_AT" \
    --workers "$WORKERS" \
    --num-pool-workers "$NUM_POOL_WORKERS" \
    --solution-temperature "$SOLUTION_TEMPERATURE" \
    --command-timeout "$COMMAND_TIMEOUT" \
    --shell-init-timeout "$SHELL_INIT_TIMEOUT" \
    --shell-init-attempts "$SHELL_INIT_ATTEMPTS" \
    --build-workers "$BUILD_WORKERS" \
    --build-retries "$BUILD_RETRIES" \
    --verbose \
    "${EXTRA_ARGS[@]}"
