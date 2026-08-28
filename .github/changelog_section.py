"""Der Release-Body trägt nur den Abschnitt der Version, nicht die ganze Datei."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def section(changelog: str, version: str) -> str | None:
    """Alles zwischen der Überschrift und dem nächsten Abschnitt."""
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]:|\Z)",
        changelog,
        re.M | re.S,
    )
    return match.group(1).strip() if match else None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Aufruf: changelog_section.py <tag>", file=sys.stderr)
        return 2
    tag = argv[1]
    version = tag[1:] if tag.startswith("v") else tag
    path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    body = section(path.read_text(encoding="utf-8"), version)
    if not body:
        print(f"Kein Abschnitt für {version} in CHANGELOG.md", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
