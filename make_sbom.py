#!/usr/bin/env python3
"""Generate a software bill of materials for the packaged executable.

Run at build time (see .github/workflows/build.yml), where the actual
Python/Tcl/Tk versions being frozen into the .exe are knowable, rather than
guessed at afterward. Storage Scanner imports no third-party runtime
package — but PyInstaller's --onefile bundle still embeds a full CPython
interpreter and the Tcl/Tk library Tkinter depends on, and a genuinely
honest SBOM should disclose *those*, since a CVE in either would affect
this shipped binary. Build-only tooling (PyInstaller, Pillow) is listed
separately, clearly marked as not present in the shipped binary's own code.

Output is a CycloneDX 1.5-shaped JSON document (https://cyclonedx.org/) —
widely supported by SBOM tooling, and simple enough to construct by hand
for a dependency graph this small without pulling in a generator library.
"""

import argparse
import json
import platform
import sys
import tkinter
import uuid
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version as pkg_version


# Optional, lazily-imported runtime packages (see history.py's matplotlib
# guard and storage_scanner/csv_to_*.py): PyInstaller only bundles one if
# it was actually installed in the build environment, so the SBOM lists
# exactly whichever of these were present -- never a fixed list that could
# claim a package is in a build it isn't.
_OPTIONAL_RUNTIME_PACKAGES = ("matplotlib", "pyarrow", "openpyxl")


def _pkg(name):
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        return None


def build_sbom(app_version):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    components = [
        {
            "type": "application",
            "name": "Storage Scanner",
            "version": app_version,
            "description": "Disk-usage analyzer for Windows and macOS.",
        },
        {
            "type": "framework",
            "name": "cpython",
            "version": platform.python_version(),
            "description": "Python interpreter, frozen into the executable by PyInstaller.",
            "scope": "required",
        },
        {
            "type": "library",
            "name": "tcl",
            "version": str(tkinter.Tcl().eval("info patchlevel")),
            "description": "Bundled with the frozen Python interpreter; backs the Tkinter GUI.",
            "scope": "required",
        },
        {
            "type": "library",
            "name": "tk",
            "version": str(tkinter.TkVersion),
            "description": "Bundled with the frozen Python interpreter; backs the Tkinter GUI.",
            "scope": "required",
        },
    ]

    for optional_package in _OPTIONAL_RUNTIME_PACKAGES:
        found_version = _pkg(optional_package)
        if found_version:
            components.append({
                "type": "library",
                "name": optional_package,
                "version": found_version,
                "description": "Optional feature dependency, bundled because it was installed at build time.",
                "scope": "optional",
            })

    for build_tool in ("pyinstaller", "pillow"):
        found_version = _pkg(build_tool)
        if found_version:
            components.append({
                "type": "application",
                "name": build_tool,
                "version": found_version,
                "description": "Build-time only — not present in the shipped executable's own code.",
                "scope": "excluded",
            })

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": now,
            "component": components[0],
        },
        "components": components,
        "_notes": (
            "Storage Scanner has no required third-party runtime dependencies. "
            "The 'required' components above are bundled by PyInstaller's "
            "--onefile packaging (the Python interpreter and the Tcl/Tk "
            "library Tkinter depends on), not pip packages the app imports. "
            "The 'optional' components, if any, back individual optional "
            "features and are only listed when they were installed at build "
            "time. The 'excluded' components are build-time tooling only."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-version", default="unknown",
        help="Version string to record for the application component (e.g. a git tag).",
    )
    parser.add_argument(
        "--output", default="sbom.json",
        help="Path to write the SBOM JSON to (default: sbom.json).",
    )
    args = parser.parse_args()

    sbom = build_sbom(args.app_version)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(sbom, f, indent=2)
        f.write("\n")

    print(f"Wrote SBOM to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
