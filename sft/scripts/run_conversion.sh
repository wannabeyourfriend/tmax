#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# ── Conversion pipeline launcher ──────────────────────────────────────
#
# Converts Terminus-2 traces into SWE-agent format for SFT training.
#
# Usage:
#   bash scripts/run_conversion.sh                        # full pipeline
#   bash scripts/run_conversion.sh --sample 1000          # 1K per source
#   bash scripts/run_conversion.sh --sample-frac 0.01     # 1% per source
#
# Sources are defined in preprocessing/config/sources.yaml.
# Output goes to preprocessing/output/.

NUM_WORKERS="${NUM_WORKERS:-$(nproc)}"
OUTPUT_DIR="${OUTPUT_DIR:-preprocessing/output}"
MAX_TURNS="${MAX_TURNS:-20}"

echo "=== Terminus-2 -> SWE-Agent Conversion Pipeline ==="
echo "  Workers:    ${NUM_WORKERS}"
echo "  Output:     ${OUTPUT_DIR}"
echo "  Max turns:  ${MAX_TURNS}"
echo ""

python -m preprocessing.pipeline \
    --num-workers "${NUM_WORKERS}" \
    --output-dir "${OUTPUT_DIR}" \
    --max-turns "${MAX_TURNS}" \
    "$@"

echo ""
echo "=== Done. Report: ${OUTPUT_DIR}/conversion_report.json ==="
