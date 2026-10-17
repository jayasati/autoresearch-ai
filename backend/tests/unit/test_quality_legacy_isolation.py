"""S1 regression guard: the legacy pipeline is untouched by report-quality-v1.

The new package exists, but nothing outside it imports it, the app does not load
it, the API surface is the stage-26 baseline (routes pinned from the `71e6b7e`
OpenAPI dump), and the pipeline gate stays off. Behavioural legacy coverage is the
existing orchestrator, verifier, extractor and finalizer suites, which S1 does not
modify.
"""

import ast
import subprocess
import sys
from pathlib import Path

from app.core.config import Settings

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

BASELINE_ROUTES = {
    ("/", "get"),
    ("/api/health", "get"),
    ("/api/health/database", "get"),
    ("/api/health/ready", "get"),
    ("/api/v1/evaluation/benchmark", "get"),
    ("/api/v1/research", "get"),
    ("/api/v1/research/start", "post"),
    ("/api/v1/research/{research_id}", "get"),
    ("/api/v1/research/{research_id}/claims", "post"),
    ("/api/v1/research/{research_id}/evidence", "get"),
    ("/api/v1/research/{research_id}/finalize", "post"),
    ("/api/v1/research/{research_id}/report", "get"),
    ("/api/v1/research/{research_id}/sources", "get"),
    ("/api/v1/research/{research_id}/verify", "post"),
    ("/api/v1/system/capabilities", "get"),
    ("/api/v1/system/ping", "get"),
}


def test_no_module_outside_the_package_imports_it():
    offenders = []
    for path in APP_ROOT.rglob("*.py"):
        if "quality" in path.relative_to(APP_ROOT).parts[:2]:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(name.startswith("app.services.quality") for name in names):
                offenders.append(str(path.relative_to(APP_ROOT)))
    assert offenders == []


def test_the_application_does_not_load_the_package():
    code = (
        "import sys, app.main; "
        "print(any(m.startswith('app.services.quality') for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=APP_ROOT.parent,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


def test_api_surface_is_the_stage_26_baseline(client):
    spec = client.get("/openapi.json").json()
    routes = {(path, method) for path, item in spec["paths"].items() for method in item}
    assert routes == BASELINE_ROUTES
    start = spec["components"]["schemas"]["ResearchStartRequest"]["properties"]
    assert set(start) == {"topic", "mode", "depth"}
    assert not [name for name in spec["components"]["schemas"] if "Quality" in name]


def test_pipeline_gate_is_off_by_default(clean_env, monkeypatch):
    for key in ("REPORT_QUALITY_ENABLED",):
        monkeypatch.delenv(key, raising=False)
    assert Settings(_env_file=None).REPORT_QUALITY_ENABLED is False
