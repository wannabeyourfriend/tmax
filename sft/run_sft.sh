#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# ── Config ───────────────────────────────────────────────────────────────────
MODEL=Qwen/Qwen3.5-4B
OUTPUT_DIR=./output
ACCELERATE_CONFIG=configs/accelerate_fsdp_8xh200.yaml
NUM_GPUS=8

# Data
SUBSETS="dataset_adapters skill_based_easy skill_based_medium skill_based_mixed"
SEED=42
SAMPLE_FRAC=0.001  # uncomment for a quick test run

# Training parameters. Match nemontron-terminal-8B
GLOBAL_BATCH_SIZE=128
MAX_LENGTH=65536 # 32768 * 2
NUM_EPOCHS=2
LR=2e-5

# Logging / saving (fractional = ratio of total steps; 0.05 ≈ every 0.1 epoch)
LOGGING_STEPS=0.01
SAVE_STEPS=0.05

# ── Launch ───────────────────────────────────────────────────────────────────
accelerate launch \
    --config_file "$ACCELERATE_CONFIG" \
    train.py \
    --model_name_or_path "$MODEL" \
    --output_dir "$OUTPUT_DIR" \
    --subsets $SUBSETS \
    --num_gpus "$NUM_GPUS" \
    --max_length "$MAX_LENGTH" \
    --num_train_epochs "$NUM_EPOCHS" \
    --learning_rate "$LR" \
    --global_batch_size "$GLOBAL_BATCH_SIZE" \
    --logging_steps "$LOGGING_STEPS" \
    --save_steps "$SAVE_STEPS" \
    --seed "$SEED" \
    --dataset_num_proc 1 \
    ${SAMPLE_FRAC:+--sample_frac "$SAMPLE_FRAC"}
