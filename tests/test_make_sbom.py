import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from make_sbom import build_sbom


def test_sbom_has_cyclonedx_shape():
    sbom = build_sbom("v1.2.3")
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert sbom["serialNumber"].startswith("urn:uuid:")
    assert isinstance(sbom["components"], list)


def test_app_component_carries_the_given_version():
    sbom = build_sbom("v1.2.3")
    app = sbom["components"][0]
    assert app["name"] == "Storage Scanner"
    assert app["version"] == "v1.2.3"


def test_bundled_runtime_components_are_marked_required():
    sbom = build_sbom("v1.2.3")
    by_name = {c["name"]: c for c in sbom["components"]}
    for name in ("cpython", "tcl", "tk"):
        assert by_name[name]["scope"] == "required"
        assert by_name[name]["version"]  # non-empty


def test_build_only_tools_are_marked_excluded_when_present():
    sbom = build_sbom("v1.2.3")
    by_name = {c["name"]: c for c in sbom["components"]}
    # pyinstaller/pillow are dev dependencies of this very repo, so they
    # should be importable in the test environment and show up here.
    for name in ("pyinstaller", "pillow"):
        if name in by_name:
            assert by_name[name]["scope"] == "excluded"


def test_sbom_is_json_serializable():
    import json

    json.dumps(build_sbom("v1.2.3"))
