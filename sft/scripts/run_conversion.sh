#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# ── Full conversion pipeline ─────────────────────────────────────────
#
# Converts every registered source (Terminus-2 traces, the Sera SWE-agent
# corpus, plus the m-a-p/TerminalTraj traces) into SFT parquets and
# pass-throughs the already-vanillux-formatted skill_tax_..._thinking_all
# subset. Sources are defined in preprocessing/config/sources.yaml.
#
# Defaults to the Vanillux2Agent harness (short system + mini-swe-agent
# instance template + single-bash tool spec). Use `--harness tassie` to
# reproduce the legacy tmax-sft-full-20260409 framing byte-for-byte
# (TassieAgent persistent-bash system prompt, bare task in user, same
# tools spec). Both harnesses write the same row schema:
#   messages | tools | source | metadata
#
# Usage:
#   bash scripts/run_conversion.sh
#   bash scripts/run_conversion.sh --upload                # also push to HF
#   bash scripts/run_conversion.sh --upload --public       # push as public
#   bash scripts/run_conversion.sh --include-partial       # keep truncated traces
#   bash scripts/run_conversion.sh --harness tassie \
#       --repo osieosie/tmax-sft-full-tassie-20260513      # legacy re-build
#
# Output: output/preprocessing/terminus2_vanillux_full_20260513/

NUM_WORKERS="$(nproc)"
OUTPUT_DIR="${OUTPUT_DIR:-output/preprocessing/terminus2_vanillux_full_20260513}"
MAX_TURNS=999
NUM_EXAMPLES=3
HF_REPO="${HF_REPO:-osieosie/tmax-sft-full-20260513}"
HARNESS="${HARNESS:-vanillux}"
UPLOAD=true
UPLOAD_FLAGS=""
INCLUDE_PARTIAL=false

# Parse our flags, pass the rest through to pipeline.py
PIPELINE_EXTRA=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --upload)            UPLOAD=true; shift ;;
        --public)            UPLOAD_FLAGS="${UPLOAD_FLAGS} --public"; shift ;;
        --repo)              HF_REPO="$2"; shift 2 ;;
        --output-dir)        OUTPUT_DIR="$2"; shift 2 ;;
        --harness)           HARNESS="$2"; shift 2 ;;
        --include-partial)   INCLUDE_PARTIAL=true; shift ;;
        *)                   PIPELINE_EXTRA="${PIPELINE_EXTRA} $1"; shift ;;
    esac
done

PARTIAL_FLAG=""
if [ "${INCLUDE_PARTIAL}" = true ]; then
    PARTIAL_FLAG="--include-partial"
fi

echo "=== Conversion pipeline (${HARNESS} harness) ==="
echo "  Workers:    ${NUM_WORKERS}"
echo "  Output:     ${OUTPUT_DIR}"
echo "  Max turns:  ${MAX_TURNS}"
echo "  Examples:   ${NUM_EXAMPLES} per source"
echo "  Partial:    ${INCLUDE_PARTIAL}"
echo "  HF repo:    ${HF_REPO}"
echo ""

# shellcheck disable=SC2086
python -m preprocessing.pipeline \
    --num-workers "${NUM_WORKERS}" \
    --output-dir "${OUTPUT_DIR}" \
    --max-turns "${MAX_TURNS}" \
    --num-examples "${NUM_EXAMPLES}" \
    --harness "${HARNESS}" \
    ${PARTIAL_FLAG} \
    ${PIPELINE_EXTRA}

echo ""
echo "=== Done. Full report: ${OUTPUT_DIR}/conversion_report.json ==="

if [ "${UPLOAD}" = true ]; then
    echo ""
    # shellcheck disable=SC2086
    bash scripts/upload_data_to_hf.sh --repo "${HF_REPO}" --input-dir "${OUTPUT_DIR}" ${UPLOAD_FLAGS}
else
    echo "=== To upload to HF:  bash scripts/upload_data_to_hf.sh --repo ${HF_REPO} --input-dir ${OUTPUT_DIR} ==="
fi
