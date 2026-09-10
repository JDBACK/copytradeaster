"""Offline line-budget gate for agent maps.

Usage: python scripts/check_doc_budgets.py [--manifest scripts/doc-budgets.json]

Compares each mapped file against its ceiling. Exit 0 when all fit,
exit 1 listing every excess file. When the gate goes red: relocate
foreign content to its home tier first, condense second, raise the
ceiling (with justification) only when the words need the space.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    manifest_arg = "--manifest"
    manifest_path = ROOT / "scripts" / "doc-budgets.json"
    if manifest_arg in sys.argv:
        manifest_path = ROOT / sys.argv[sys.argv.index(manifest_arg) + 1]
    ceilings: dict[str, int] = json.loads(manifest_path.read_text())
    failed = False
    for rel, ceiling in ceilings.items():
        target = ROOT / rel
        if not target.exists():
            print(f"MISSING {rel} (budget {ceiling})")
            failed = True
            continue
        actual = sum(1 for _ in target.open(encoding="utf-8"))
        mark = "OK  " if actual <= ceiling else "OVER"
        print(f"{mark} {rel}: {actual}/{ceiling}")
        failed = failed or actual > ceiling
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
