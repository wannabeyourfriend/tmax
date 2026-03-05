from fnmatch import fnmatch

import pyarrow as pa
import pyarrow.parquet as pq
from datasets import Dataset, concatenate_datasets
from datasets.table import InMemoryTable
from huggingface_hub import HfApi, hf_hub_download

REPO_ID = "nvidia/Nemotron-Terminal-Corpus"

ALL_SUBSETS = [
    "dataset_adapters",
    "skill_based_easy",
    "skill_based_medium",
    "skill_based_mixed",
]

SUBSET_PATTERNS = {
    "dataset_adapters": "dataset_adapters/*.parquet",
    "skill_based_easy": "synthetic_tasks/skill_based/easy/*/data_filtered.parquet",
    "skill_based_medium": "synthetic_tasks/skill_based/medium/*/data_filtered.parquet",
    "skill_based_mixed": "synthetic_tasks/skill_based/mixed/*/data_filtered.parquet",
}

TRAIN_COLUMNS = {"messages"}


def _resolve_parquet_paths(subset: str) -> list[str]:
    """Return the Hub-relative paths of parquet files for *subset*."""
    pattern = SUBSET_PATTERNS[subset]
    all_files = HfApi().list_repo_files(REPO_ID, repo_type="dataset")
    paths = sorted(f for f in all_files if fnmatch(f, pattern))
    if not paths:
        raise FileNotFoundError(
            f"No parquet files matching '{pattern}' for subset '{subset}' in {REPO_ID}"
        )
    return paths


def _ensure_downloaded(paths: list[str], cache_dir: str | None = None) -> list[str]:
    """Download *paths* from the Hub (cached after first call) and return local paths."""
    return [
        hf_hub_download(REPO_ID, p, repo_type="dataset", cache_dir=cache_dir)
        for p in paths
    ]

#* Hacky way to download and read parquet files from large terminal data repository.
def _read_parquet_files(local_paths: list[str]) -> pa.Table:
    """Read parquet files into a single Arrow table.

    Uses ``ParquetFile.iter_batches`` (the C++ FileReader path) with a small
    batch size so that nested columns never exceed pyarrow's single-array size
    limit -- avoiding the ``ArrowNotImplementedError: Nested data conversions
    not implemented for chunked array outputs`` bug.
    """
    batches: list[pa.RecordBatch] = []
    for path in local_paths:
        pf = pq.ParquetFile(path)
        batches.extend(pf.iter_batches(batch_size=1024, columns=["conversations"]))
    return pa.Table.from_batches(batches)


def load_terminal_corpus(
    subsets: list[str] | None = None,
    sample_frac: float | None = None,
    seed: int = 42,
    cache_dir: str | None = None,
) -> Dataset:
    """Load and prepare Nemotron-Terminal-Corpus for SFT training.

    Args:
        subsets: Which subsets to include (default: all four).
        sample_frac: If set, randomly sample this fraction from each subset.
        seed: Random seed used for sub-sampling.
        cache_dir: Override the HuggingFace cache directory.

    Returns:
        A single HF Dataset with a ``messages`` column ready for TRL.
    """
    if subsets is None:
        subsets = list(ALL_SUBSETS)

    parts: list[Dataset] = []
    for name in subsets:
        hub_paths = _resolve_parquet_paths(name)
        local_paths = _ensure_downloaded(hub_paths, cache_dir)
        table = _read_parquet_files(local_paths)
        ds = Dataset(InMemoryTable(table))

        if sample_frac is not None and 0 < sample_frac < 1:
            n = max(1, int(len(ds) * sample_frac))
            ds = ds.shuffle(seed=seed).select(range(n))

        ds = ds.rename_column("conversations", "messages")
        drop = [c for c in ds.column_names if c not in TRAIN_COLUMNS]
        ds = ds.remove_columns(drop)
        parts.append(ds)

    dataset = concatenate_datasets(parts)
    return dataset
