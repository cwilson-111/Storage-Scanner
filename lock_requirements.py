"""Regenerate the hash-locked requirement files CI installs.

requirements-*.txt name the tools and their exact versions; each
requirements-*.lock beside it adds everything those pull in, every package
pinned with the SHA-256 of each file PyPI has for it, so `pip install
--require-hashes` installs exactly that and nothing else. The locks are
universal: one file serves Windows, macOS and Linux and Python 3.11 to
3.14, with environment markers for what only some of them need.

Run after changing a requirements-*.txt (needs uv: `pip install uv`):

    python lock_requirements.py
"""

import os
import subprocess
import sys

LOCKS = {
    "requirements-dev.lock": ["requirements-dev.txt"],
    # What the Data build and its test job install: the dev tools too.
    "requirements-data.lock": ["requirements-dev.txt", "requirements-data.txt"],
    "requirements-audit.lock": ["requirements-audit.txt"],
}


def main():
    for output, inputs in LOCKS.items():
        subprocess.run(
            [
                os.environ.get("UV", "uv"),  # or the path to uv
                "pip",
                "compile",
                "--quiet",
                "--universal",
                "--generate-hashes",
                "--python-version",
                "3.11",
                "--custom-compile-command",
                "python lock_requirements.py",
                *inputs,
                "--output-file",
                output,
            ],
            check=True,
        )
        print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
