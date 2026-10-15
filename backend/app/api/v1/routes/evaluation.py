"""
The evaluation endpoint: serve the committed benchmark artifacts.

Read-only over the files the experiment runner wrote under
`BENCHMARK_DATA_DIR/results/<set>/`. The numbers are served verbatim from those
artifacts — never recomputed at request time — so this endpoint cannot show a
result the repository does not contain. When no artifact exists the response is a
404, not an empty table that could read as "measured: zero".
"""

import json
import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError
from app.schemas.evaluation import BenchmarkArtifactResponse

logger = logging.getLogger(__name__)

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]

#: The fields of the v1 experiment header worth serving; everything else in the
#: artifact (per-row results) is reachable in the repository, not over this API.
EXPERIMENT_HEADER_FIELDS = (
    "benchmark_run_id",
    "set_name",
    "set_version",
    "set_fingerprint",
    "arms",
    "items",
    "started_at",
    "completed_at",
)


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Benchmark artifact unreadable: %s", path)
        return None


def _latest_artifact(results_dir: Path, *, reevaluation: bool) -> tuple[Path, dict] | None:
    """The newest artifact directory of the requested kind, by timestamped name.

    Directory names begin with a UTC timestamp, so lexical order is temporal
    order. A directory that has no readable `raw_results.json` is skipped rather
    than failing the whole response — a partial artifact is preserved history,
    not a servable result.
    """
    if not results_dir.is_dir():
        return None
    candidates = sorted(
        (
            d
            for d in results_dir.iterdir()
            if d.is_dir() and d.name.endswith("-v2eval") == reevaluation
        ),
        key=lambda d: d.name,
        reverse=True,
    )
    for directory in candidates:
        payload = _load_json(directory / "raw_results.json")
        if payload is not None and ("comparison" in payload or "primary_matched" in payload):
            return directory, payload
    return None


@router.get("/benchmark", response_model=BenchmarkArtifactResponse)
async def benchmark_results(
    settings: SettingsDep,
    set_name: str = Query(default="test", pattern="^(development|validation|test)$"),
) -> BenchmarkArtifactResponse:
    """The committed benchmark comparison for one set, served from its artifacts."""
    results_dir = Path(settings.BENCHMARK_DATA_DIR) / "results" / set_name

    experiment = _latest_artifact(results_dir, reevaluation=False)
    if experiment is None:
        raise NotFoundError(
            f"No benchmark artifacts for the {set_name} set.",
            details={"searched": str(results_dir)},
        )
    directory, payload = experiment

    external = None
    reevaluation = _latest_artifact(results_dir, reevaluation=True)
    if reevaluation is not None:
        _, reeval_payload = reevaluation
        external = {
            "primary_matched": reeval_payload.get("primary_matched"),
            "aggregates_v2": reeval_payload.get("aggregates_v2"),
            "provenance": reeval_payload.get("reevaluation"),
        }

    header = payload.get("experiment", {})
    return BenchmarkArtifactResponse(
        set_name=set_name,
        source_dir=directory.name,
        experiment={k: header[k] for k in EXPERIMENT_HEADER_FIELDS if k in header},
        comparison=payload.get("comparison", []),
        aggregates=payload.get("aggregates", {}),
        external=external,
    )
