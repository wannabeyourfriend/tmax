# Technical Report: Conversion Pipeline Improvements

**Date:** 2026-03-10
**Scope:** All iterative improvements to the Terminus-2 to SWE-agent conversion pipeline, from the initial 1% teaser run through the current production-ready state.

---

## 1. Executive Summary

The conversion pipeline transforms raw Terminus-2 agent traces into SWE-agent style tool-calling format for SFT training. Across multiple iterations, we improved the pipeline's yield from **30.6% to an estimated 85-90%** of input traces through a series of targeted fixes to JSON extraction, filtering, and output cleaning.

| Metric | Before | After |
|--------|--------|-------|
| Overall yield | 30.6% (1,166/3,812) | ~85-90% estimated |
| `json_extraction_failed` drops | 2,038 | 48 |
| `<think>` tag double-wrapping | Present in 99.8% of kept Nemotron traces | 0 |
| Consecutive assistant duplicates | ~8,000 occurrences | 0 (merged) |
| Harness error noise in tool output | ~210,000 tool messages | 0 (turns deleted) |
| Shell prompt UUIDs in tool output | Present in all Nemotron traces | Stripped |

### Source Datasets

- `open-thoughts/OpenThoughts-Agent-v1-SFT` (~15.2K traces, GLM-4.6 teacher)
- `nvidia/Nemotron-Terminal-Corpus` (~366K traces across 4 subsets, DeepSeek-V3.2 teacher)

---

## 2. Improvement History

### Phase 1: JSON Extraction for `<think>`-Wrapped Content

**Problem identified:** The initial 1% teaser run showed only 30.6% yield. The dominant drop reason was `json_extraction_failed` (2,038 of 2,646 dropped traces, 77%). Root cause analysis on the dropped traces revealed that 100% of JSON extraction failures occurred in messages wrapped with `<think>...</think>` tags.

The Nemotron dataset (DeepSeek-V3.2 teacher) wraps all assistant content in `<think>` tags. The original JSON extractor's brace-matching strategy searches for the first `{` in the content, but mathematical prose inside `<think>` blocks often contains braces (e.g., `{-4}`, `{1, 2, 3}`) that confuse the matcher.

**Two distinct patterns were identified:**

**Pattern A** — JSON after `</think>` (4,264 failing messages):
```
<think>
[prose with math braces like n^{-4} + n^{-10}]
</think>

{
  "analysis": "...",
  "commands": [...],
  "task_complete": false
}
```
The brace matcher locks onto a math brace inside `<think>` instead of the actual JSON after the closing tag.

**Pattern B** — JSON fields merged inside `<think>` (1,199 failing messages):
```
<think>
[long analysis prose]…final answer: \"The limit does not exist.\"",
  "plan": "Write solution to file",
  "commands": [...],
  "task_complete": false
}
</think>
```
The model's reasoning prose flows directly into the JSON body. There is no opening `{` or `"analysis"` key -- the prose IS the analysis value, and the remaining JSON keys (`plan`, `commands`, `task_complete`) follow inline.

**Solution implemented** in `json_extraction.py`:

- **Strategy 4:** Strip `<think>...</think>` tags, then apply strategies 1-3 to the text after `</think>`. Handles Pattern A.
- **Strategy 5:** When no JSON is found after `</think>`, extract `commands`, `task_complete`, and `plan` fields from inside the think block via targeted regex parsing (bracket matching for arrays, string extraction for values). The leading prose becomes the `analysis` field. Handles Pattern B.

**Results:**
- 1,990 of 2,038 `json_extraction_failed` traces recovered (97.6%)
- Yield improved from 30.6% to 69.2% on the 1% sample

**Collateral fix:** The `<think>` tag stripping also fixed a pre-existing **double-wrapping bug**. Before the fix, 99.8% of kept Nemotron traces had `<think>` tags embedded inside `reasoning_content`, which gets rendered as `<think>...</think>` by the Qwen chat template -- producing `<think><think>content</think></think>` during tokenization. After the fix, `reasoning_content` contains only the prose, with zero `<think>` tag leaks (verified across 6,587 reasoning_content fields).

### Phase 2: `no_task_complete` Investigation and `--include-partial` Flag

**Problem:** After Phase 1, `no_task_complete` became the dominant drop reason (1,128 traces, 95.9% of remaining drops). Investigation showed:

- **Zero** traces had `task_complete: true` anywhere in the raw data. All 1,128 are genuinely incomplete -- the agent never finished.
- All dropped traces end with an assistant message (no harness confirmation prompt), indicating **conversation truncation at the Nemotron turn limit**, not agent refusal.
- The impact is heavily concentrated in `skill_based_medium` (666/893 = 74.5% drop rate) -- a difficulty effect where harder tasks complete less often within the allotted turns.
- 242 traces (21%) have reasoning suggesting near-completion, but the remaining 886 are genuinely mid-work.

**Solution:** Added a configurable `--include-partial` CLI flag that keeps truncated traces with a `no_task_complete` warning flag instead of dropping them. This allows downstream training to decide how to weight partial trajectories.

```bash
# Default: strict (drops incomplete traces)
python -m preprocessing.pipeline

# Keep incomplete traces, flagged for downstream filtering
python -m preprocessing.pipeline --include-partial
```

### Phase 3: Shell Prompt Stripping

**Problem:** Tool output (terminal logs) contained container-specific shell prompts with UUID hostnames:
```
root@f6151565-0057-4ac0-813f-6ddce99d660f:/app# ls -la
total 4
...
root@f6151565-0057-4ac0-813f-6ddce99d660f:/app#
```
These UUIDs are instance-specific noise that adds tokens without information value.

**Solution** in `builders.py`: Added a regex `_SHELL_PROMPT_RE` that matches `root@<long-hostname>:<path>#` patterns (hostnames >= 7 characters to avoid false positives on short legitimate hostnames). Strips prompts while preserving actual command output, heredoc content (`>` prompts), and everything else. Reduces tool output size by ~24% on typical messages.

### Phase 4: Submit Truncation

**Problem:** The Nemotron harness sends an "Are you sure you want to mark the task as complete?" confirmation after the first submit. The agent responds and re-confirms, creating a duplicate pattern at the end of most traces:
```
assistant(submit) → assistant(reasoning) → assistant(submit)
```
This teaches the model to double-submit, which is undesirable.

**Solution** in `convert.py`: The conversion loop now `break`s immediately after emitting the first submit message. All subsequent messages (confirmation prompt, re-confirmation, second submit) are discarded. Result: exactly one submit per completed trace.

### Phase 5: Data Quality Cleanup (Current)

Four additional quality issues identified from manual review of the full production run (263,629 traces):

#### 5a. Strip "Current terminal state" from First User Message

**Problem:** Every first user message ended with a harness artifact:
```
\n\nCurrent terminal state:\nCurrent Terminal Screen:\nroot@<uuid>:/app#
```
This is container-specific context noise (263,629 affected traces -- all of them).

**Fix** in `convert.py`: The code already separated the terminal state from the task description via `STATE_DELIM`. The fix was simply removing the 2 lines that re-appended it to the user message.

#### 5b. Merge Consecutive Reasoning-Only Assistant Messages

**Problem:** ~7,974 cases of consecutive assistant messages where the first has `reasoning_content` but no `tool_calls` (the original agent sent `commands: []`), followed by another assistant with tool calls. This creates a non-standard role sequence.

**Fix** in `convert.py`: Introduced a `pending_reasoning` buffer. When an assistant message has reasoning but no tool calls, the reasoning is buffered instead of being emitted as a standalone message. When the next assistant message with tool calls arrives, the buffered reasoning is prepended to it. This collapses `reasoning_only → reasoning+bash` into a single clean `reasoning+bash` message.

#### 5c. Filter Traces Containing Ctrl+C

**Problem:** ~27,579 tool calls across the full dataset use `C-c` (Ctrl+C interrupt). These represent the agent getting stuck and interrupting a running process -- behavior we do not want to teach the model during SFT.

**Fix:** In `convert.py`, a `has_ctrl_c` flag is tracked during conversion. In `filters.py`, traces with this flag set are dropped with reason `contains_ctrl_c`.

#### 5d. Delete Harness Error Turns

**Problem:** ~210,333 tool messages contain harness error text:
```
Previous response had parsing errors:
ERROR: No valid JSON found in response
WARNINGS: - No valid JSON object found

Please fix these issues and provide a proper JSON response.
```
These are injected by the Nemotron harness when the original agent's JSON response could not be parsed. The harness likely **did not execute** the agent's command -- it rejected the response. In our converted format where tool calls are always valid, this error text is misleading training signal.

**Analysis confirmed:** 100% of these error tool messages contain ONLY the harness error -- no real terminal output mixed in (verified on a 5,000-trace sample: 6,956/6,956 are error-only).

**Fix** in `convert.py`: Added `is_harness_error()` detection in `builders.py`. During conversion, when the next user message (potential tool result) is detected as a harness error, the **entire turn** (both the assistant message and the error tool message) is skipped. The trace continues from the next assistant message. This preserves trace structure while removing turns where no real execution occurred.

---

## 3. Files Modified

| File | Changes |
|------|---------|
| `json_extraction.py` | Added strategies 4 and 5 for `<think>` tag handling; `_extract_from_text()`, `_extract_think_body()`, `_extract_json_array()`, `_extract_json_string()`, `_find_matching_bracket()` |
| `convert.py` | Removed terminal state re-append; added reasoning buffering; added `has_ctrl_c` tracking; added harness error turn deletion; submit truncation |
| `builders.py` | Added `_SHELL_PROMPT_RE` for prompt stripping; harness error stripping in `build_tool_result()`; added `is_harness_error()` |
| `filters.py` | Added `require_task_complete` parameter; added `contains_ctrl_c` mandatory filter; added `no_task_complete` warning flag |
| `pipeline.py` | Threaded `require_task_complete` and `--include-partial` CLI flag; added strategy 4/5 counts; added `conversion_report.txt` saving |
| `report.py` | Added `save_path` parameter for plain-text report export |
| `scripts/run_conversion_teaser.sh` | Added `--upload`, `--include-partial` flags; fixed bash array expansion bug |
| `scripts/upload_to_hf.sh` | New script for pushing converted data to HuggingFace Hub |

---

## 4. Remaining Known Issues

### 4a. Tool Output Command Echo (Deferred)

Tool output includes the typed command echoed back (e.g., `ls -la\ntotal 0\n...`) because the raw data is a terminal log. The command is already present in the `tool_call`, making the echo redundant. However, stripping echoes from interleaved multi-command terminal output -- especially with heredocs, continuation prompts, and multi-line commands -- is fragile and error-prone. This was intentionally deferred.

### 4b. SWE-Bench Patch Format Tasks

Some traces from `dataset_adapters` include task descriptions like: *"please first localize the bug based on the issue statement, generate SEARCH/REPLACE edits to fix the issue, and save the diff to a file named `/app/solution.patch`"*. These are adapted from SWE-bench and may have different quality characteristics. They are kept in the dataset but flagged for awareness.

### 4c. Data Mixing Ratio

The Nemotron corpus dominates ~24:1 over OpenThoughts. No rebalancing is applied at the pipeline level -- this is deferred to the training configuration (sampling weights, curriculum).

---

## 5. Verification

All fixes were validated with unit tests covering:

- Pure JSON, prose+JSON, `<think>`+JSON (Pattern A), `<think>` merged (Pattern B), non-JSON content
- Downstream builder compatibility (`build_reasoning_content`, `build_tool_calls`, `build_submit_messages`)
- Parsed dict identity between old and new extraction for existing traces
- Shell prompt stripping with UUID hostnames, venv prefixes, heredoc preservation
- Submit truncation (single submit, confirmation loop discarded)
- Harness error turn deletion (entire turn removed, good turns preserved)
- Consecutive assistant merging (reasoning buffered and prepended)
- C-c detection in metadata

Real-data validation on the 1% sample (5,253 previously-failing messages):
- 5,220 recovered (99.3%)
- 100% of recovered messages pass through downstream builders without error
- Zero `<think>` tag leaks in reasoning_content

---

## 6. Pipeline Usage

```bash
# 1% teaser run (quick validation)
bash scripts/run_conversion_teaser.sh

# Full run
bash scripts/run_conversion.sh

# Include partial (truncated) traces
bash scripts/run_conversion_teaser.sh --include-partial

# Convert + upload to HuggingFace
bash scripts/run_conversion_teaser.sh --upload --public
bash scripts/upload_to_hf.sh --input-dir <output_dir>  # standalone upload
```

Output directory contains:
- `*.parquet` -- per-source kept traces (training-ready)
- `*_dropped.jsonl` -- per-source dropped traces (for diagnosis)
- `conversion_report.json` -- machine-readable statistics
- `conversion_report.txt` -- human-readable report (ANSI-stripped)
