#!/bin/bash
# Orchestrator for the dataset-comparison suite.
#
# Pipeline:
#   1. Ingest baselines  (scripts/run_ingest_et.sh,
#                         scripts/run_ingest_openthoughts.sh,
#                         scripts/run_ingest_termigen.sh,
#                         scripts/run_ingest_terminaltraj.sh)
#   2. Classify external tasks into OUR taxonomy via an LLM
#      (scripts/run_classify_taxonomy.sh)
#   3. Run our solution harness on each baseline with the same model as the 10k
#      (scripts/run_generate_solutions_{et,openthoughts,termigen,terminaltraj}.sh)
#   4. Compare (python -m rl_data.comparison.cli)
#
# Each stage can be skipped with SKIP_* env vars. Typical flow:
#
#   SKIP_SOLVE=1 bash rl_data/scripts/comparison/run_comparison.sh    # quick dry-run
#   bash rl_data/scripts/comparison/run_comparison.sh                 # full pipeline
#
# Env toggles:
#   OURS_TASKS_DIR         Default: rl_data/output/tasks_skill_tax_20260401_10k
#   ET_TASKS_DIR           Default: rl_data/output/tasks_endless_terminals
#   OT_TASKS_DIR           Default: rl_data/output/tasks_openthoughts_agent_rl
#   TG_TASKS_DIR           Default: rl_data/output/tasks_termigen
#   TT_TASKS_DIR           Default: rl_data/output/tasks_terminaltraj
#   COMPARE_OUT_DIR        Default: rl_data/output/comparison
#   MODEL                  Default: gemini/gemini-3-flash-preview
#
# Stage skips:
#   SKIP_INGEST_ET=1, SKIP_INGEST_OT=1, SKIP_INGEST_TG=1, SKIP_INGEST_TT=1
#   SKIP_CLASSIFY=1
#   SKIP_SOLVE_ET=1, SKIP_SOLVE_OT=1, SKIP_SOLVE_TG=1, SKIP_SOLVE_TT=1
#   SKIP_COMPARE=1

set -euo pipefail

OURS_TASKS_DIR="${OURS_TASKS_DIR:-rl_data/output/tasks_skill_tax_20260401_10k}"
ET_TASKS_DIR="${ET_TASKS_DIR:-rl_data/output/tasks_endless_terminals}"
OT_TASKS_DIR="${OT_TASKS_DIR:-rl_data/output/tasks_openthoughts_agent_rl}"
TG_TASKS_DIR="${TG_TASKS_DIR:-rl_data/output/tasks_termigen}"
TT_TASKS_DIR="${TT_TASKS_DIR:-rl_data/output/tasks_terminaltraj}"
COMPARE_OUT_DIR="${COMPARE_OUT_DIR:-rl_data/output/comparison}"
MODEL="${MODEL:-gemini/gemini-3-flash-preview}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== Comparison pipeline ==="
echo "  ours         : $OURS_TASKS_DIR"
echo "  ET           : $ET_TASKS_DIR"
echo "  OT           : $OT_TASKS_DIR"
echo "  TermiGen     : $TG_TASKS_DIR"
echo "  TerminalTraj : $TT_TASKS_DIR"
echo "  model        : $MODEL"
echo "  out-dir      : $COMPARE_OUT_DIR"
echo

# ── 1. Ingest ──────────────────────────────────────────────────────────
# if [[ "${SKIP_INGEST_ET:-0}" != "1" ]]; then
#   echo ">>> 1a. Ingesting endless-terminals"
#   ET_DST="$ET_TASKS_DIR" bash "$SCRIPT_DIR/run_ingest_et.sh"
# fi
# if [[ "${SKIP_INGEST_OT:-0}" != "1" ]]; then
#   echo ">>> 1b. Ingesting OpenThoughts-Agent-v1-RL"
#   OT_DST="$OT_TASKS_DIR" bash "$SCRIPT_DIR/run_ingest_openthoughts.sh"
# fi
# if [[ "${SKIP_INGEST_TG:-0}" != "1" ]]; then
#   echo ">>> 1c. Ingesting TermiGen (ucsb-mlsec/terminal-bench-env)"
#   TG_DST="$TG_TASKS_DIR" bash "$SCRIPT_DIR/run_ingest_termigen.sh"
# fi
# if [[ "${SKIP_INGEST_TT:-0}" != "1" ]]; then
#   echo ">>> 1d. Ingesting TerminalTraj (m-a-p/TerminalTraj-5k-instances)"
#   TT_DST="$TT_TASKS_DIR" bash "$SCRIPT_DIR/run_ingest_terminaltraj.sh"
# fi

# # ── 2. Classify into our taxonomy ──────────────────────────────────────
# if [[ "${SKIP_CLASSIFY:-0}" != "1" ]]; then
#   echo ">>> 2. Classifying external tasks into our taxonomy"
#   CLASSIFY_DIRS="$ET_TASKS_DIR $OT_TASKS_DIR $TG_TASKS_DIR $TT_TASKS_DIR" \
#       CLASSIFY_MODEL="$MODEL" \
#       bash "$SCRIPT_DIR/run_classify_taxonomy.sh"
# fi

# # ── 3. Solve ───────────────────────────────────────────────────────────
# if [[ "${SKIP_SOLVE_ET:-0}" != "1" ]]; then
#   echo ">>> 3a. Solving endless-terminals (prefer Slurm for the real run)"
#   bash "$SCRIPT_DIR/run_generate_solutions_et.sh"
# fi
# if [[ "${SKIP_SOLVE_OT:-0}" != "1" ]]; then
#   echo ">>> 3b. Solving OpenThoughts-Agent-v1-RL (prefer Slurm for the real run)"
#   bash "$SCRIPT_DIR/run_generate_solutions_openthoughts.sh"
# fi
# if [[ "${SKIP_SOLVE_TG:-0}" != "1" ]]; then
#   echo ">>> 3c. Solving TermiGen (prefer Slurm for the real run)"
#   bash "$SCRIPT_DIR/run_generate_solutions_termigen.sh"
# fi
# if [[ "${SKIP_SOLVE_TT:-0}" != "1" ]]; then
#   echo ">>> 3d. Solving TerminalTraj (prefer Slurm for the real run)"
#   bash "$SCRIPT_DIR/run_generate_solutions_terminaltraj.sh"
# fi

# ── 4. Compare ─────────────────────────────────────────────────────────
if [[ "${SKIP_COMPARE:-0}" != "1" ]]; then
  echo ">>> 4. Running comparison"
  BASELINE_ARGS=()
  if [[ -d "$ET_TASKS_DIR" ]]; then
    BASELINE_ARGS+=(--baseline "endless_terminals:$ET_TASKS_DIR")
  fi
  if [[ -d "$OT_TASKS_DIR" ]]; then
    BASELINE_ARGS+=(--baseline "openthoughts_agent_rl:$OT_TASKS_DIR")
  fi
  if [[ -d "$TG_TASKS_DIR" ]]; then
    BASELINE_ARGS+=(--baseline "termigen:$TG_TASKS_DIR")
  fi
  if [[ -d "$TT_TASKS_DIR" ]]; then
    BASELINE_ARGS+=(--baseline "terminaltraj:$TT_TASKS_DIR")
  fi

  uv run python -m rl_data.comparison.cli \
      --reference "skill_tax:$OURS_TASKS_DIR" \
      "${BASELINE_ARGS[@]}" \
      --model "$MODEL" \
      --out-dir "$COMPARE_OUT_DIR"
fi

echo
echo "Done. See ${COMPARE_OUT_DIR}/main/ for headline figures + summary_table.md."
