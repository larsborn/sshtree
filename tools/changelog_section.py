#!/usr/bin/env python3
"""Print the CHANGELOG.md section of one version, for the release notes.

    python tools/changelog_section.py 0.2.6       # or v0.2.6

Prints the body of "## [0.2.6] - ..." without its heading and without the
link-reference lines at the end of the file.  Exits 1 when the version has no
section, so a release build fails loudly rather than publishing empty notes.
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\]")


def section(text: str, version: str) -> str | None:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        match = HEADING.match(line)
        if not match:
            continue
        if start is not None:  # the next version heading ends the section
            return "\n".join(lines[start:i]).strip()
        if match.group("version") == version:
            start = i + 1
    if start is None:
        return None
    body = lines[start:]
    while body and (not body[-1].strip() or body[-1].startswith("[")):
        body.pop()  # trailing blank lines and the link references
    return "\n".join(body).strip()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    version = argv[0].lstrip("vV")
    body = section(CHANGELOG.read_text(encoding="utf-8"), version)
    if not body:
        print(f"error: no CHANGELOG section for {version}", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
