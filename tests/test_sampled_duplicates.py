"""Tests for sampled duplicate matching logic."""

import os

from storage_scanner.cleanup_recommendations import (
    get_sampled_duplicates_from_groups,
    is_sampled_duplicate,
)
from storage_scanner.models import FileNode, Node

MB = 1024 * 1024


def _files(*specs):
    """FileNodes for (name, size) rows of one scanned folder."""
    folder = Node(os.path.join(os.sep, "data"), "data")
    return [FileNode(folder, folder.add_file(name, size)) for name, size in specs]


class TestSampledDuplicateDetection:
    """Test identification of sampled vs full-content duplicate matches."""

    def test_is_sampled_duplicate_below_threshold(self):
        """Files up to 3 MB are compared in full."""
        assert not is_sampled_duplicate(1 * MB)
        assert not is_sampled_duplicate(2 * MB)
        assert not is_sampled_duplicate(3 * MB)  # exactly 3 MB

    def test_is_sampled_duplicate_above_threshold(self):
        """Files over 3 MB are only sampled (first/middle/last)."""
        assert is_sampled_duplicate(3 * MB + 1)
        assert is_sampled_duplicate(10 * MB)
        assert is_sampled_duplicate(100 * MB)

    def test_get_sampled_duplicates_from_groups_empty_targets(self):
        """Empty target list returns zero sampled."""
        (node_a,) = _files(("a.txt", 5 * MB))
        groups = [(5 * MB, "digest", [node_a])]
        count, nodes = get_sampled_duplicates_from_groups([], groups)
        assert count == 0
        assert len(nodes) == 0

    def test_get_sampled_duplicates_from_groups_no_sampled_groups(self):
        """If no groups are sampled, no targets are identified as sampled."""
        node_a, node_b = _files(("a.txt", 2 * MB), ("b.txt", 2 * MB))
        groups = [(2 * MB, "digest1", [node_a, node_b])]

        count, nodes = get_sampled_duplicates_from_groups([node_a], groups)
        assert count == 0
        assert len(nodes) == 0

    def test_get_sampled_duplicates_from_groups_sampled_group(self):
        """If a group is sampled and target is in it, it's identified."""
        node_a, node_b = _files(("a.txt", 5 * MB), ("b.txt", 5 * MB))
        groups = [(5 * MB, "digest1", [node_a, node_b])]

        count, nodes = get_sampled_duplicates_from_groups([node_a], groups)
        assert count == 1
        assert node_a in nodes

    def test_a_target_is_matched_through_another_view_of_the_same_file(self):
        """The duplicate scan and the window each make their own FileNode
        for a file; the same row must still count as the same file."""
        node_a, node_b = _files(("a.txt", 5 * MB), ("b.txt", 5 * MB))
        groups = [(5 * MB, "digest1", [node_a, node_b])]
        same_file = FileNode(node_a.parent, node_a.index)

        count, nodes = get_sampled_duplicates_from_groups([same_file], groups)

        assert count == 1
        assert same_file in nodes

    def test_get_sampled_duplicates_from_groups_mixed(self):
        """Some groups sampled, some not; only sampled group targets count."""
        node_small_a, node_small_b, node_large_a, node_large_b = _files(
            ("small_a.txt", 2 * MB),
            ("small_b.txt", 2 * MB),
            ("large_a.txt", 10 * MB),
            ("large_b.txt", 10 * MB),
        )
        groups = [
            (2 * MB, "digest1", [node_small_a, node_small_b]),
            (10 * MB, "digest2", [node_large_a, node_large_b]),
        ]

        # Select one from each group
        targets = [node_small_a, node_large_a]
        count, nodes = get_sampled_duplicates_from_groups(targets, groups)

        assert count == 1  # Only node_large_a is sampled
        assert node_large_a in nodes
        assert node_small_a not in nodes

    def test_get_sampled_duplicates_from_groups_multiple_targets_same_group(self):
        """Multiple targets from the same sampled group all count."""
        node_a, node_b, node_c = _files(("a.txt", 10 * MB), ("b.txt", 10 * MB), ("c.txt", 10 * MB))
        groups = [(10 * MB, "digest1", [node_a, node_b, node_c])]

        targets = [node_a, node_b]  # Select 2 out of 3 from the sampled group
        count, nodes = get_sampled_duplicates_from_groups(targets, groups)

        assert count == 2
        assert node_a in nodes
        assert node_b in nodes
        assert node_c not in nodes

    def test_get_sampled_duplicates_from_groups_target_not_in_group(self):
        """Target node not in any group is not identified as sampled."""
        node_in_group, node_not_in_group = _files(
            ("in_group.txt", 10 * MB), ("not_in_group.txt", 10 * MB)
        )
        groups = [(10 * MB, "digest1", [node_in_group])]

        count, nodes = get_sampled_duplicates_from_groups([node_not_in_group], groups)

        assert count == 0
        assert node_not_in_group not in nodes
