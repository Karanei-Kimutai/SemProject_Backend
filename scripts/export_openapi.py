"""Write docs/openapi.json from the running code. The Android app generates its client from it."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("JWT_SECRET", "export-only-secret-at-least-32-characters")
from app.main import app  # noqa: E402

out = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"wrote {out}")
