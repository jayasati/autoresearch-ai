"""
Loading the benchmark sets, and enforcing the freeze.

    data/benchmarks/development.json   refinable from development-run observations
    data/benchmarks/validation.json    refinable, used to tune thresholds once
    data/benchmarks/test.json          FROZEN — the hash in test.freeze.json pins it
    data/benchmarks/test.freeze.json   the freeze manifest
    data/benchmarks/README.md          the ground-truth methodology

The freeze is a mechanism, not a convention: `load_set("test")` recomputes the
canonical content hash and refuses to return a test set that no longer matches its
manifest. Nothing downstream can accidentally evaluate against an edited test set,
and an intentional edit forces a visible, deliberate re-freeze with a version bump —
which the methodology document says may happen only *before* final evaluation, and
never in response to benchmark results.
"""

import json
import logging
from datetime import date
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError
from app.schemas.benchmark import BenchmarkSet

logger = logging.getLogger(__name__)

SET_NAMES = ("development", "validation", "test")


class BenchmarkDatasetError(AppError):
    """A dataset file is missing or does not satisfy the schema."""

    status_code = 500
    code = "benchmark_dataset_invalid"
    message = "A benchmark dataset file is missing or invalid."


class FrozenSetViolation(AppError):
    """The test set's content no longer matches its freeze manifest.

    This is the error that makes the no-modification rule real. It does not say who
    changed what; it says the final test set is not the one that was frozen, and no
    evaluation may run against it until that is resolved — by restoring the frozen
    content, never by re-freezing to match the edit after results exist.
    """

    status_code = 500
    code = "frozen_set_violation"
    message = "The final test set does not match its freeze manifest."


def benchmark_directory(settings: Settings | None = None) -> Path:
    return Path((settings or get_settings()).BENCHMARK_DATA_DIR)


def load_set(name: str, settings: Settings | None = None) -> BenchmarkSet:
    """Load and validate one set; for the test set, enforce the freeze."""
    if name not in SET_NAMES:
        raise BenchmarkDatasetError(
            f"Unknown benchmark set {name!r}; the sets are {', '.join(SET_NAMES)}.",
            details={"set": name},
        )
    directory = benchmark_directory(settings)
    path = directory / f"{name}.json"
    if not path.exists():
        raise BenchmarkDatasetError(
            f"Benchmark set file not found: {path}", details={"path": str(path)}
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        benchmark_set = BenchmarkSet.model_validate(payload)
    except (ValueError, TypeError) as exc:
        raise BenchmarkDatasetError(
            f"Benchmark set {name!r} failed validation: {exc}", details={"path": str(path)}
        ) from exc

    if benchmark_set.name != name:
        raise BenchmarkDatasetError(
            f"File {path.name} declares set {benchmark_set.name!r}.",
            details={"path": str(path)},
        )

    if name == "test":
        _enforce_freeze(benchmark_set, directory)

    return benchmark_set


def load_freeze_manifest(directory: Path) -> dict:
    path = directory / "test.freeze.json"
    if not path.exists():
        raise FrozenSetViolation(
            "The test set has no freeze manifest. It must be frozen before any "
            "evaluation runs against it.",
            details={"expected": str(path)},
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _enforce_freeze(benchmark_set: BenchmarkSet, directory: Path) -> None:
    manifest = load_freeze_manifest(directory)
    actual = benchmark_set.fingerprint
    if manifest.get("sha256") != actual:
        raise FrozenSetViolation(
            details={
                "frozen_sha256": manifest.get("sha256"),
                "actual_sha256": actual,
                "frozen_at": manifest.get("frozen_at"),
                "version_frozen": manifest.get("version"),
            }
        )
    if manifest.get("version") != benchmark_set.version:
        raise FrozenSetViolation(
            f"The test set declares version {benchmark_set.version} but "
            f"{manifest.get('version')} was frozen.",
        )


def write_freeze_manifest(settings: Settings | None = None) -> dict:
    """Freeze the current test set: compute its canonical hash and write the manifest.

    Deliberately the only writer of `test.freeze.json`, and deliberately dumb: it
    pins whatever the test set currently says. The *rules* about when freezing is
    allowed — before final evaluation, never in response to results — live in the
    methodology document and in review, because no function can know why it is
    being called.
    """
    directory = benchmark_directory(settings)
    payload = json.loads((directory / "test.json").read_text(encoding="utf-8"))
    benchmark_set = BenchmarkSet.model_validate(payload)
    manifest = {
        "set": "test",
        "version": benchmark_set.version,
        "sha256": benchmark_set.fingerprint,
        "item_count": len(benchmark_set.items),
        "frozen_at": date.today().isoformat(),
        "policy": (
            "This set is frozen. It may not be modified in response to benchmark "
            "results. Any pre-evaluation change requires a version bump, a re-freeze, "
            "and a DEVELOPMENT_LOG entry saying what changed and why."
        ),
    }
    path = directory / "test.freeze.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    logger.info("Froze test set v%s (%s)", benchmark_set.version, benchmark_set.fingerprint[:12])
    return manifest
