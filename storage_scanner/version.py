"""The running app's version.

Stamped at build time by .github/workflows/build.yml from the git tag
being released. Running from source (or a local build.bat run, which
doesn't stamp this) falls back to this placeholder — storage_scanner.
update_check recognizes it as "not a real release" and never shows an
update notice for it, since nagging a source/dev run to "update" makes no
sense.
"""

__version__ = "0.0.0-dev"
