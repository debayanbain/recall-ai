"""Export the FastAPI OpenAPI schema to docs/openapi.{json,yaml}.

Run: uv run python scripts/export_openapi.py
Keeps the committed spec in sync with the code — the app is the source of truth.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.main import app

OUT_DIR = Path(__file__).resolve().parent.parent / "docs"


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    schema = app.openapi()

    # Cookie session auth: FastAPI cannot infer this from a Cookie() parameter.
    schema.setdefault("components", {})["securitySchemes"] = {
        "sessionCookie": {
            "type": "apiKey",
            "in": "cookie",
            "name": "recall_session",
            "description": "Signed JWT session cookie set by /api/v1/auth/google/callback.",
        }
    }

    json_path = OUT_DIR / "openapi.json"
    json_path.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"wrote {json_path} ({len(schema['paths'])} paths)")

    try:
        import yaml
    except ImportError:
        print("PyYAML not installed - skipped openapi.yaml")
        return
    yaml_path = OUT_DIR / "openapi.yaml"
    yaml_path.write_text(yaml.safe_dump(schema, sort_keys=False, width=100))
    print(f"wrote {yaml_path}")


if __name__ == "__main__":
    main()
