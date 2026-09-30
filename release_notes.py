#!/usr/bin/env python3
"""Pick one release's section out of CHANGELOG.md, for its GitHub release notes.

The release job in .github/workflows/build.yml runs this with the tag being
released and hands the result to the release step as `body_path`. A tag
with no section, or an empty one, fails the release rather than publishing
a page with no notes, which is what every release from v1.04 to v1.11.0 had.

A section starts at a `## <tag>` heading (anything after the tag on that
line, such as the date, is ignored) and runs up to the next `## ` heading;
`###` subheadings stay inside it. The heading itself is left out, since
the release page already shows the tag as its title.

    python release_notes.py v1.11.0 --output dist/release-notes.md
"""

import argparse
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent / "CHANGELOG.md"


def section(changelog_text, tag):
    """The text of tag's section with its heading and surrounding blank
    lines removed ("" if it's empty), or None if there's no such section."""
    body = None
    for line in changelog_text.splitlines():
        if line.startswith("## "):
            if body is not None:
                break
            words = line[3:].split()
            if words and words[0] == tag:
                body = []
        elif body is not None:
            body.append(line)

    if body is None:
        return None
    return "\n".join(body).strip("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tag", help="The release's tag, e.g. v1.11.0")
    parser.add_argument("--changelog", type=Path, default=CHANGELOG, help="default: %(default)s")
    parser.add_argument("--output", type=Path, help="Write here instead of stdout")
    args = parser.parse_args(argv)

    notes = section(args.changelog.read_text(encoding="utf-8"), args.tag)
    if not notes or not notes.strip():
        problem = "no" if notes is None else "an empty"
        print(
            f"{args.changelog.name} has {problem} '## {args.tag}' section; "
            "write the release notes there before tagging.",
            file=sys.stderr,
        )
        return 1

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(notes + "\n", encoding="utf-8")
    else:
        # Bytes, so ≥, → and friends survive a Windows console or pipe.
        sys.stdout.buffer.write((notes + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
