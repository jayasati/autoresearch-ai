"""
Benchmark dataset infrastructure: the three question sets, their schemas, and the
freeze that protects the final test set.

No results live here, and none were generated to build it — this stage is the
measuring instrument, not the measurement. The ground-truth methodology is in
`data/benchmarks/README.md`.
"""

from app.services.benchmark.dataset import (
    SET_NAMES,
    BenchmarkDatasetError,
    FrozenSetViolation,
    benchmark_directory,
    load_freeze_manifest,
    load_set,
    write_freeze_manifest,
)
from app.services.benchmark.metrics import METRICS_VERSION

__all__ = [
    "BenchmarkDatasetError",
    "FrozenSetViolation",
    "METRICS_VERSION",
    "SET_NAMES",
    "benchmark_directory",
    "load_freeze_manifest",
    "load_set",
    "write_freeze_manifest",
]
