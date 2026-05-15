#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# ── Upload converted dataset to Hugging Face ─────────────────────────
#
# Uploads the parquet files from a pipeline output directory to a HF
# dataset repo.  The dataset viewer ("preview") works automatically
# for parquet files once pushed.
#
# Usage:
#   bash scripts/upload_to_hf.sh                       # defaults below
#   bash scripts/upload_to_hf.sh --input-dir preprocessing/terminus2_sweagent_1pct
#   bash scripts/upload_to_hf.sh --repo osieosie/tmax-sft-preview --private
#
# Requirements:
#   - huggingface-cli login  (or HF_TOKEN env var)
#   - Python with datasets + huggingface_hub in PATH

REPO_ID="osieosie/tmax-sft-full-20260310"
INPUT_DIR="/gpfs/scrubbed/osey/tmax/sft/output/preprocessing/terminus2_sweagent_full_20260310"
PRIVATE="false"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)       REPO_ID="$2"; shift 2 ;;
        --input-dir)  INPUT_DIR="$2"; shift 2 ;;
        --private)    PRIVATE="true"; shift ;;
        --public)     PRIVATE="false"; shift ;;
        *)            echo "Unknown arg: $1"; exit 1 ;;
    esac
done

echo "=== Upload to Hugging Face ==="
echo "  Repo:      ${REPO_ID}"
echo "  Input dir: ${INPUT_DIR}"
echo "  Private:   ${PRIVATE}"
echo ""

python - --repo "${REPO_ID}" --input-dir "${INPUT_DIR}" --private "${PRIVATE}" << 'PYTHON_EOF'
import argparse
import sys
from pathlib import Path

from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--input-dir", required=True)
    p.add_argument("--private", default="true")
    args = p.parse_args()

    input_dir = Path(args.input_dir)
    repo_id = args.repo
    private = args.private.lower() == "true"

    parquet_files = sorted(input_dir.glob("*.parquet"))
    if not parquet_files:
        print(f"No parquet files found in {input_dir}", file=sys.stderr)
        sys.exit(1)

    api = HfApi()

    api.create_repo(repo_id, repo_type="dataset", private=private, exist_ok=True)
    print(f"Repo ready: https://huggingface.co/datasets/{repo_id}")

    # Upload each parquet as its own config (subset) so that sources with
    # different metadata schemas coexist without feature-mismatch errors.
    # HF Dataset Viewer auto-detects parquet files by path convention:
    #   data/{config_name}/{split}-*.parquet
    operations = []

    # Clear stale `data/` only if the folder actually exists -- otherwise
    # the commit fails with `Entry Not Found ... A file with the name "data"
    # does not exist` on a freshly created repo (rolling back the whole
    # commit, including the parquets we just uploaded).
    try:
        existing_files = api.list_repo_files(repo_id, repo_type="dataset")
    except Exception as e:
        print(f"  warn: could not list existing files ({e}); skipping cleanup", file=sys.stderr)
        existing_files = []
    if any(f.startswith("data/") for f in existing_files):
        print("  Clearing stale `data/` from previous upload")
        operations.append(CommitOperationDelete(path_in_repo="data/", is_folder=True))

    config_names = []
    for pf in parquet_files:
        config_name = pf.stem.replace("-", "_")
        config_names.append(config_name)
        path_in_repo = f"data/{config_name}/train-00000-of-00001.parquet"
        operations.append(CommitOperationAdd(
            path_in_repo=path_in_repo,
            path_or_fileobj=str(pf),
        ))
        print(f"  {pf.name} → {path_in_repo}")

    # Generate README.md with YAML configs so the Dataset Viewer
    # knows about each subset and its data files.
    yaml_configs = "configs:\n"
    for cn in config_names:
        yaml_configs += (
            f"- config_name: {cn}\n"
            f"  data_files:\n"
            f"  - split: train\n"
            f"    path: data/{cn}/train-*.parquet\n"
        )
    default = config_names[0] if config_names else "default"
    yaml_configs += f"default_config_name: {default}\n"

    readme = f"---\n{yaml_configs}---\n"
    operations.append(CommitOperationAdd(
        path_in_repo="README.md",
        path_or_fileobj=readme.encode(),
    ))

    for report_name in ("conversion_report.json", "filter_report.json"):
        report_path = input_dir / report_name
        if report_path.exists():
            operations.append(CommitOperationAdd(
                path_in_repo=report_name,
                path_or_fileobj=str(report_path),
            ))

    print(f"\nUploading {len(parquet_files)} config(s) in a single commit ...")
    api.create_commit(
        repo_id=repo_id,
        repo_type="dataset",
        operations=operations,
        commit_message=f"Upload {len(parquet_files)} converted sources",
    )

    print(f"\nDone. {len(parquet_files)} config(s) at: https://huggingface.co/datasets/{repo_id}")


if __name__ == "__main__":
    main()
PYTHON_EOF
