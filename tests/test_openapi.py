import json
from pathlib import Path

from app.main import app


def test_committed_openapi_is_current():
    committed = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
    assert json.loads(committed.read_text()) == json.loads(json.dumps(app.openapi(), sort_keys=True)), \
        "Run `python scripts/export_openapi.py` and commit docs/openapi.json"
