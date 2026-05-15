"""Drop SFT rows that leak the literal ``<tool_call>`` token.

Some upstream traces (Nemotron, TerminalTraj, OpenThoughts, ...) occasionally
emit the assistant tag ``<tool_call>`` as a *string literal* inside
``content`` / ``reasoning_content`` instead of through the native
``tool_calls`` field. When mixed into SFT data this teaches the student to
hallucinate that tag as plain text, which then never gets parsed as a real
tool invocation at inference time. The fix here is mechanical: drop any
row where *any* message's ``content`` or ``reasoning_content`` contains
the substring ``<tool_call>``.

Reads every ``*.parquet`` under ``--input-dir`` (the output dir of
``preprocessing.pipeline``), writes filtered parquets to ``--output-dir``,
and emits a ``filter_report.json`` with per-source kept / removed counts.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

# Literal substring we are filtering on. Match is case-sensitive on purpose:
# the assistant chat template tag is always lowercase, and case-insensitive
# matching would risk dropping legitimate prose mentioning "Tool Call".
BAD_SUBSTRING = "<tool_call>"


def _chunk_row_hits(messages_chunk: pa.ListArray, needle: str) -> np.ndarray:
    """Per-row boolean mask: ``True`` if any message in this row matches.

    Operates on a single chunk so we never have to materialise a >2 GiB
    ListArray (which overflows the underlying int32 offsets).
    """
    struct = messages_chunk.values
    content = struct.field("content")
    reasoning = struct.field("reasoning_content")

    c_hit = pc.match_substring(content, needle)
    r_hit = pc.match_substring(reasoning, needle)
    msg_hit = pc.or_kleene(c_hit, r_hit)
    msg_hit = pc.fill_null(msg_hit, False)
    msg_hit_np = msg_hit.to_numpy(zero_copy_only=False).astype(np.int64)

    offsets = np.asarray(messages_chunk.offsets)
    n_rows = len(messages_chunk)
    if msg_hit_np.size == 0:
        return np.zeros(n_rows, dtype=bool)
    # Cumulative-diff trick handles empty rows (offsets[i]==offsets[i+1]) by
    # producing 0 for those rows, which `np.add.reduceat` would not.
    cumsum = np.concatenate([[0], np.cumsum(msg_hit_np)])
    row_hits = cumsum[offsets[1:]] - cumsum[offsets[:-1]]
    return row_hits > 0


def filter_parquet(input_path: Path, output_path: Path, needle: str) -> dict:
    """Filter a single parquet file. Returns a stats dict."""
    t0 = time.time()
    tbl = pq.read_table(str(input_path))
    messages_chunked = tbl.column("messages")

    # Process chunk-by-chunk to avoid concatenating a ListArray whose flat
    # values exceed 2 GiB (Arrow ListArray uses int32 offsets).
    keep_parts: list[np.ndarray] = []
    for chunk in messages_chunked.chunks:
        chunk_hits = _chunk_row_hits(chunk, needle)
        keep_parts.append(~chunk_hits)
    keep_mask = (
        np.concatenate(keep_parts) if keep_parts else np.zeros(0, dtype=bool)
    )

    n_total = int(len(keep_mask))
    n_kept = int(keep_mask.sum())
    n_removed = n_total - n_kept

    kept_tbl = tbl.filter(pa.array(keep_mask))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(kept_tbl, str(output_path))

    return {
        "input_path": str(input_path),
        "output_path": str(output_path),
        "input_rows": n_total,
        "kept_rows": n_kept,
        "removed_rows": n_removed,
        "removed_pct": round(100.0 * n_removed / n_total, 3) if n_total else 0.0,
        "elapsed_seconds": round(time.time() - t0, 2),
    }


def run_filter(
    input_dir: Path,
    output_dir: Path,
    needle: str = BAD_SUBSTRING,
) -> dict:
    """Filter every ``*.parquet`` in *input_dir* and write to *output_dir*.

    Skips ``*_dropped.jsonl`` (those are the original drop sidecars) and any
    other non-parquet files. Returns the aggregate report dict.
    """
    parquet_files = sorted(p for p in input_dir.glob("*.parquet") if p.is_file())
    if not parquet_files:
        raise SystemExit(f"No parquet files found in {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    per_source: dict[str, dict] = {}
    total_in = 0
    total_kept = 0
    total_removed = 0

    for pf in parquet_files:
        out = output_dir / pf.name
        logger.info("Filtering %s ...", pf.name)
        stats = filter_parquet(pf, out, needle)
        per_source[pf.stem] = stats
        total_in += stats["input_rows"]
        total_kept += stats["kept_rows"]
        total_removed += stats["removed_rows"]
        logger.info(
            "  %s: %d -> %d (-%d, %.2f%%) in %.1fs",
            pf.name,
            stats["input_rows"],
            stats["kept_rows"],
            stats["removed_rows"],
            stats["removed_pct"],
            stats["elapsed_seconds"],
        )

    aggregate = {
        "input_rows": total_in,
        "kept_rows": total_kept,
        "removed_rows": total_removed,
        "removed_pct": round(100.0 * total_removed / total_in, 3) if total_in else 0.0,
    }
    report = {
        "needle": needle,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "per_source": per_source,
        "aggregate": aggregate,
    }
    report_path = output_dir / "filter_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    logger.info("Wrote %s", report_path)
    return report


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input-dir", required=True, type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument(
        "--needle",
        default=BAD_SUBSTRING,
        help=f"Substring that disqualifies a row (default: {BAD_SUBSTRING!r}).",
    )
    return p.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args()
    report = run_filter(args.input_dir, args.output_dir, args.needle)
    agg = report["aggregate"]
    print()
    print("=" * 64)
    print(f"Filter needle: {args.needle!r}")
    print(f"Input dir:     {report['input_dir']}")
    print(f"Output dir:    {report['output_dir']}")
    print()
    print(
        f"Aggregate: kept {agg['kept_rows']:,} / {agg['input_rows']:,}  "
        f"(removed {agg['removed_rows']:,}, {agg['removed_pct']:.2f}%)"
    )
    print("=" * 64)


if __name__ == "__main__":
    main()
