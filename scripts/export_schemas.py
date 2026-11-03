"""Write JSON Schemas for the contracts into contracts/schemas/.

Pydantic models are the source; the JSON Schemas are the language-neutral copy for the web app and for
other teams. `--check` fails if the committed schemas are out of date (run in CI).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from kcd_core.contracts import PAYLOADS, Envelope, Manifest, PolicyCatalog, Release

OUT = Path(__file__).resolve().parents[1] / "contracts" / "schemas"


def render() -> dict[Path, str]:
    files: dict[Path, str] = {}

    def put(rel: str, schema: dict[str, object]) -> None:
        files[OUT / rel] = json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    put("envelope.schema.json", Envelope.model_json_schema(by_alias=True))
    put("manifest.schema.json", Manifest.model_json_schema())
    put("policy_catalog.schema.json", PolicyCatalog.model_json_schema())
    put("release.schema.json", Release.model_json_schema())
    for type_, model in PAYLOADS.items():
        put(f"messages/{type_.value}.schema.json", model.model_json_schema())
    return files


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for path, text in render().items():
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(path.relative_to(OUT.parents[1]))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
    if stale:
        print("schemas out of date, run: uv run python scripts/export_schemas.py", *stale, sep="\n  ")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
