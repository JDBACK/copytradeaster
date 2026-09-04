"""Check that every relative .md link in docs resolves to an existing file.

Usage: python scripts/check_doc_links.py [--strict]
Exit 0 when all links resolve; exit 1 listing missing targets otherwise.
--strict also fails on links with dead #fragment anchors.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"\]\(([^)]+)\)")

SCAN = [
    ROOT / "AGENTS.md",
    ROOT / "PRODUCT.md",
    ROOT / "README.md",
    *sorted((ROOT / "docs").rglob("*.md")),
    ROOT / "tests" / "AGENTS.md",
    ROOT / "scripts" / "AGENTS.md",
    ROOT / "miniapp" / "AGENTS.md",
]


def iter_links(path: Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for m in LINK_RE.finditer(line):
            target = m.group(1).strip()
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            out.append((n, target))
    return out


def check(strict: bool) -> list[str]:
    errors: list[str] = []
    for path in SCAN:
        if not path.exists():
            errors.append(f"{path}: scanned file itself missing")
            continue
        for lineno, target in iter_links(path):
            file_part, _, anchor = target.partition("#")
            if not file_part.endswith(".md") or not file_part:
                continue
            resolved = (path.parent / file_part).resolve()
            try:
                resolved.relative_to(ROOT)
            except ValueError:
                errors.append(f"{path}:{lineno}: link escapes repo: {target}")
                continue
            if not resolved.is_file():
                errors.append(f"{path}:{lineno}: missing target: {target}")
            elif strict and anchor:
                text = resolved.read_text(encoding="utf-8")
                heading = re.sub(r"[^\w\s-]", "", anchor.replace("-", " ")).strip().lower()
                if heading and heading not in text.lower().replace("#", " "):
                    errors.append(f"{path}:{lineno}: dead anchor: {target}")
    return errors


def main() -> int:
    strict = "--strict" in sys.argv[1:]
    errors = check(strict)
    if errors:
        print(f"{len(errors)} broken doc link(s):")
        for e in errors:
            print(f"  {e}")
        return 1
    print(f"doc links OK ({len(SCAN)} files scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
