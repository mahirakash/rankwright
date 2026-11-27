"""The schema must ship inside the wheel, and match the repo-root copy."""
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "scripts" / "py"


def test_packaged_schemas_match_repo_root():
    for name in ("rankwright.config.schema.json", "report.schema.json"):
        packaged = (PY / "rankwright" / "schemas" / name).read_bytes()
        assert packaged == (ROOT / "schemas" / name).read_bytes(), (
            f"{name} drifted: copy schemas/{name} into scripts/py/rankwright/schemas/"
        )


def test_wheel_contains_schemas(tmp_path):
    pytest.importorskip("build")
    out = tmp_path / "dist"
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--outdir", str(out), str(PY)], check=True, capture_output=True)
    whl = next(out.glob("rankwright-*.whl"))
    assert "rankwright/schemas/rankwright.config.schema.json" in zipfile.ZipFile(whl).namelist()
