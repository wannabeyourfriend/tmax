#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# ── Filter out rows that leak literal <tool_call> ────────────────────
#
# Reads every parquet in INPUT_DIR (defaults to the
# terminus2_vanillux_full_20260513 conversion output), drops any row whose
# messages contain the literal substring "<tool_call>" in `content` or
# `reasoning_content`, writes filtered parquets to OUTPUT_DIR (suffixed
# `_bad_tool_call_filtered`), and uploads them as a separate HF dataset
# (default repo: tmax-sft-full-20260513-bad-tool-call-filtered).
#
# Usage:
#   bash scripts/run_filter_bad_tool_call.sh              # filter + upload
#   bash scripts/run_filter_bad_tool_call.sh --no-upload  # filter only
#   bash scripts/run_filter_bad_tool_call.sh --public     # push as public
#   bash scripts/run_filter_bad_tool_call.sh --repo me/x  # override repo
#

INPUT_DIR="${INPUT_DIR:-output/preprocessing/terminus2_vanillux_full_20260513}"
OUTPUT_DIR="${OUTPUT_DIR:-output/preprocessing/terminus2_vanillux_full_20260513_bad_tool_call_filtered}"
HF_REPO="${HF_REPO:-osieosie/tmax-sft-full-20260513-bad-tool-call-filtered}"
NEEDLE="${NEEDLE:-<tool_call>}"
UPLOAD=true
UPLOAD_FLAGS=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --input-dir)   INPUT_DIR="$2"; shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2"; shift 2 ;;
        --repo)        HF_REPO="$2"; shift 2 ;;
        --needle)      NEEDLE="$2"; shift 2 ;;
        --no-upload)   UPLOAD=false; shift ;;
        --upload)      UPLOAD=true; shift ;;
        --public)      UPLOAD_FLAGS="${UPLOAD_FLAGS} --public"; shift ;;
        --private)     UPLOAD_FLAGS="${UPLOAD_FLAGS} --private"; shift ;;
        *)             echo "Unknown arg: $1"; exit 1 ;;
    esac
done

echo "=== Bad-<tool_call> filter ==="
echo "  Input dir:  ${INPUT_DIR}"
echo "  Output dir: ${OUTPUT_DIR}"
echo "  Needle:     ${NEEDLE}"
echo "  HF repo:    ${HF_REPO}"
echo "  Upload:     ${UPLOAD}"
echo ""

python -m preprocessing.filter_bad_tool_call \
    --input-dir "${INPUT_DIR}" \
    --output-dir "${OUTPUT_DIR}" \
    --needle "${NEEDLE}"

echo ""
echo "=== Filter report: ${OUTPUT_DIR}/filter_report.json ==="

if [ "${UPLOAD}" = true ]; then
    echo ""
    # shellcheck disable=SC2086
    bash scripts/upload_data_to_hf.sh --repo "${HF_REPO}" --input-dir "${OUTPUT_DIR}" ${UPLOAD_FLAGS}
else
    echo "=== To upload to HF:  bash scripts/upload_data_to_hf.sh --repo ${HF_REPO} --input-dir ${OUTPUT_DIR} ==="
fi
