import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from release_notes import main, section

CHANGELOG = """# Changelog

Intro text that belongs to no release.

## Unreleased

- Not out yet

## v1.10.0 — 2026-09-26

- Ten

### Fixes

- A ten fix

## v1.1.0 — 2026-09-11

- One

## v1.04 — 2026-07-07
"""


def test_a_tag_gets_its_own_section_not_one_it_is_a_prefix_of():
    assert section(CHANGELOG, "v1.1.0") == "- One"


def test_subheadings_stay_in_the_section_and_the_next_release_does_not():
    assert section(CHANGELOG, "v1.10.0") == "- Ten\n\n### Fixes\n\n- A ten fix"


def test_missing_and_empty_sections_are_told_apart():
    assert section(CHANGELOG, "v1.2.0") is None
    assert section(CHANGELOG, "v1.04") == ""


def test_main_writes_the_section_to_the_output_file(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    out = tmp_path / "dist" / "notes.md"

    code = main(["v1.10.0", "--changelog", str(changelog), "--output", str(out)])

    assert code == 0
    assert out.read_text(encoding="utf-8").startswith("- Ten\n")


def test_a_tag_without_notes_fails_the_release_and_writes_nothing(tmp_path, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    out = tmp_path / "notes.md"

    assert main(["v1.2.0", "--changelog", str(changelog), "--output", str(out)]) == 1
    assert "no '## v1.2.0' section" in capsys.readouterr().err
    assert main(["v1.04", "--changelog", str(changelog), "--output", str(out)]) == 1
    assert "an empty '## v1.04' section" in capsys.readouterr().err
    assert not out.exists()
