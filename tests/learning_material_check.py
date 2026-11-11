"""Check the learning-material input contract and record its file identity."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from public_protocol_check import strict_json

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--material", type=Path, default=ROOT / "agent" / "conversation.json")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--source-revision")
    args = parser.parse_args()
    material_path = args.material.resolve()
    report = {
        "scope": "Learning-material file input contract and identity",
        "path": str(material_path),
        "source_revision": args.source_revision,
        "passed": False,
    }
    try:
        contents = material_path.read_bytes()
        report.update(bytes=len(contents), sha256=hashlib.sha256(contents).hexdigest())
        if len(contents) > 128 * 1024:
            raise ValueError("Material exceeds the 128 KiB limit")
        material = strict_json(contents.decode("utf-8"))
        if not isinstance(material, dict):
            raise ValueError("Material must be a top-level JSON object")
        report.update(top_level_keys=list(material),
                      synthetic_declared="synthetic" in material,
                      synthetic=material.get("synthetic"), passed=True)
    except (OSError, ValueError) as error:
        report["error"] = str(error)

    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(rendered, end="")
    if args.report:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(rendered, encoding="utf-8")
        except OSError as error:
            print("Cannot write learning-material report: " + str(error), file=sys.stderr)
            return 1
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
