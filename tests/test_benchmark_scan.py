import os
import queue
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmark_scan import TreeSpec, compare_to_baseline, generate_tree, remove_tree, verify
from storage_scanner.scanner import scan

SPEC = TreeSpec(dirs=15, files=60, hardlinks=5, chain_depth=10)


@pytest.fixture
def generated(tmp_path):
    manifest = generate_tree(str(tmp_path / "gen"), SPEC, seed=7)
    yield manifest
    remove_tree(manifest)


def _scan(path):
    return scan(path, queue.SimpleQueue(), threading.Event())


def _files_and_sizes(root):
    found = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                found[os.path.relpath(path, root)] = os.path.getsize(path)
    return found


def test_scanner_matches_generated_tree(generated):
    # The real scanner against an independently known answer: rollup,
    # hard-link dedup, link non-traversal, deep and oddly named paths.
    assert generated.hardlink_extras == SPEC.hardlinks
    assert verify(_scan(generated.root), generated) == []


def test_verify_reports_a_followed_link(generated):
    if not generated.links:
        pytest.skip("this account can't create symlinks or junctions here")
    root_node = _scan(generated.root)
    links_node = next(c for c in root_node.children if c.name == "links")
    link = next(c for c in links_node.children if c.path == generated.links[0])
    link.is_dir = True  # what a scanner that traversed the link would produce

    problems = verify(root_node, generated)

    assert any("followed" in p and generated.links[0] in p for p in problems)


def test_verify_reports_a_wrong_rollup(generated):
    root_node = _scan(generated.root)
    tree = next(c for c in root_node.children if c.name == "tree")
    tree.size += 1

    assert any(p.startswith("tree/ size") for p in verify(root_node, generated))


def test_same_seed_generates_identical_tree_and_different_seed_does_not(tmp_path):
    first = generate_tree(str(tmp_path / "a"), SPEC, seed=7)
    again = generate_tree(str(tmp_path / "b"), SPEC, seed=7)
    other = generate_tree(str(tmp_path / "c"), SPEC, seed=8)
    try:
        assert _files_and_sizes(first.root) == _files_and_sizes(again.root)
        assert first.top_level == again.top_level
        assert _files_and_sizes(first.root) != _files_and_sizes(other.root)
    finally:
        for manifest in (first, again, other):
            remove_tree(manifest)


def _result(median, **overrides):
    result = {
        "schema": 1,
        "profile": "small",
        "seed": 1,
        "tree": {"files": 10, "folders": 2, "bytes": 100, "hardlink_extras": 1, "links": []},
        "platform": "Windows-11",
        "python": "3.12.0",
        "cpu_count": 8,
        "workers": None,
        "median_seconds": median,
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize(
    ("median", "regressed"), [(1.0, False), (1.25, False), (1.26, True), (0.5, False)]
)
def test_baseline_regression_threshold(median, regressed):
    assert compare_to_baseline(_result(median), _result(1.0), max_slowdown=0.25)[0] is regressed


@pytest.mark.parametrize(
    "field_override",
    [{"seed": 2}, {"profile": "medium"}, {"schema": 2}, {"tree": {"files": 11}}],
)
def test_baseline_of_a_different_tree_is_refused(field_override):
    with pytest.raises(ValueError):
        compare_to_baseline(_result(1.0), _result(1.0, **field_override), max_slowdown=0.25)


def test_baseline_from_other_environment_warns_but_still_compares():
    regressed, _message, warnings = compare_to_baseline(
        _result(1.0), _result(1.0, python="3.9.0"), max_slowdown=0.25
    )
    assert not regressed
    assert len(warnings) == 1 and "python" in warnings[0]
