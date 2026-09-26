"""Tests for sampled duplicate matching logic."""

from storage_scanner.cleanup_recommendations import (
    get_sampled_duplicates_from_groups,
    is_sampled_duplicate,
)
from storage_scanner.models import Node


class TestSampledDuplicateDetection:
    """Test identification of sampled vs full-content duplicate matches."""

    def test_is_sampled_duplicate_below_threshold(self):
        """Files up to 3 MB are compared in full."""
        assert not is_sampled_duplicate(1024 * 1024)  # 1 MB
        assert not is_sampled_duplicate(2 * 1024 * 1024)  # 2 MB
        assert not is_sampled_duplicate(3 * 1024 * 1024)  # exactly 3 MB

    def test_is_sampled_duplicate_above_threshold(self):
        """Files over 3 MB are only sampled (first/middle/last)."""
        assert is_sampled_duplicate(3 * 1024 * 1024 + 1)  # 3 MB + 1 byte
        assert is_sampled_duplicate(10 * 1024 * 1024)  # 10 MB
        assert is_sampled_duplicate(100 * 1024 * 1024)  # 100 MB

    def test_get_sampled_duplicates_from_groups_empty_targets(self):
        """Empty target list returns zero sampled."""
        groups = [(5 * 1024 * 1024, "digest", [Node("a.txt", False, 0)])]
        count, nodes = get_sampled_duplicates_from_groups([], groups)
        assert count == 0
        assert len(nodes) == 0

    def test_get_sampled_duplicates_from_groups_no_sampled_groups(self):
        """If no groups are sampled, no targets are identified as sampled."""
        node_a = Node("a.txt", False, 1024 * 1024)
        node_b = Node("b.txt", False, 1024 * 1024)
        groups = [(2 * 1024 * 1024, "digest1", [node_a, node_b])]

        count, nodes = get_sampled_duplicates_from_groups([node_a], groups)
        assert count == 0
        assert len(nodes) == 0

    def test_get_sampled_duplicates_from_groups_sampled_group(self):
        """If a group is sampled and target is in it, it's identified."""
        node_a = Node("a.txt", False, 5 * 1024 * 1024)
        node_b = Node("b.txt", False, 5 * 1024 * 1024)
        groups = [(5 * 1024 * 1024, "digest1", [node_a, node_b])]

        count, nodes = get_sampled_duplicates_from_groups([node_a], groups)
        assert count == 1
        assert node_a in nodes

    def test_get_sampled_duplicates_from_groups_mixed(self):
        """Some groups sampled, some not; only sampled group targets count."""
        # Small group (not sampled)
        node_small_a = Node("small_a.txt", False, 2 * 1024 * 1024)
        node_small_b = Node("small_b.txt", False, 2 * 1024 * 1024)

        # Large group (sampled)
        node_large_a = Node("large_a.txt", False, 10 * 1024 * 1024)
        node_large_b = Node("large_b.txt", False, 10 * 1024 * 1024)

        groups = [
            (2 * 1024 * 1024, "digest1", [node_small_a, node_small_b]),
            (10 * 1024 * 1024, "digest2", [node_large_a, node_large_b]),
        ]

        # Select one from each group
        targets = [node_small_a, node_large_a]
        count, nodes = get_sampled_duplicates_from_groups(targets, groups)

        assert count == 1  # Only node_large_a is sampled
        assert node_large_a in nodes
        assert node_small_a not in nodes

    def test_get_sampled_duplicates_from_groups_multiple_targets_same_group(self):
        """Multiple targets from the same sampled group all count."""
        node_a = Node("a.txt", False, 10 * 1024 * 1024)
        node_b = Node("b.txt", False, 10 * 1024 * 1024)
        node_c = Node("c.txt", False, 10 * 1024 * 1024)
        groups = [(10 * 1024 * 1024, "digest1", [node_a, node_b, node_c])]

        targets = [node_a, node_b]  # Select 2 out of 3 from the sampled group
        count, nodes = get_sampled_duplicates_from_groups(targets, groups)

        assert count == 2
        assert node_a in nodes
        assert node_b in nodes
        assert node_c not in nodes

    def test_get_sampled_duplicates_from_groups_target_not_in_group(self):
        """Target node not in any group is not identified as sampled."""
        node_in_group = Node("in_group.txt", False, 10 * 1024 * 1024)
        node_not_in_group = Node("not_in_group.txt", False, 10 * 1024 * 1024)

        groups = [(10 * 1024 * 1024, "digest1", [node_in_group])]

        count, nodes = get_sampled_duplicates_from_groups([node_not_in_group], groups)

        assert count == 0
        assert node_not_in_group not in nodes
