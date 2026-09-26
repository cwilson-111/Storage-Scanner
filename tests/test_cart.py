import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage_scanner.cart import CartManager
from storage_scanner.models import FileNode, Node


def _root():
    return Node("C:/root", "root")


def _file(parent, name, size=0):
    return FileNode(parent, parent.add_file(name, size))


def _dir(parent, name, size=0):
    node = Node(f"{parent.path}/{name}", name)
    node.size = size
    parent.dirs.append(node)
    return node


def test_add_and_len():
    cart = CartManager()
    root = _root()
    f = _file(root, "a.bin", size=100)

    cart.add(f, "Main tree")

    assert len(cart) == 1
    assert f in cart


def test_add_same_node_twice_updates_label_not_duplicates():
    cart = CartManager()
    root = _root()
    f = _file(root, "a.bin", size=100)

    cart.add(f, "Main tree")
    cart.add(f, "Duplicate Files")

    assert len(cart) == 1
    assert cart.items() == [(f, "Duplicate Files")]


def test_remove():
    cart = CartManager()
    root = _root()
    f = _file(root, "a.bin", size=100)
    cart.add(f, "Main tree")

    cart.remove(f)

    assert len(cart) == 0
    assert f not in cart


def test_remove_missing_node_is_a_noop():
    cart = CartManager()
    root = _root()
    f = _file(root, "a.bin", size=100)

    cart.remove(f)  # never added

    assert len(cart) == 0


def test_clear():
    cart = CartManager()
    root = _root()
    cart.add(_file(root, "a.bin", size=100), "Main tree")
    cart.add(_file(root, "b.bin", size=200), "Search & Filter")

    cart.clear()

    assert len(cart) == 0
    assert cart.items() == []


def test_add_none_is_a_noop():
    cart = CartManager()
    cart.add(None, "Main tree")
    assert len(cart) == 0


def test_total_bytes_sums_across_files_and_directories():
    cart = CartManager()
    root = _root()
    f1 = _file(root, "a.bin", size=100)
    f2 = _file(root, "b.bin", size=250)
    d = _dir(root, "SomeApp", size=5000)  # a folder's own rolled-up size

    cart.add(f1, "Main tree")
    cart.add(f2, "Search & Filter")
    cart.add(d, "Cleanup Recommendations")

    assert cart.total_bytes() == 100 + 250 + 5000


def test_resolve_effective_items_drops_a_child_nested_under_a_cart_parent():
    cart = CartManager()
    root = _root()
    folder = _dir(root, "SomeApp", size=5000)
    inside = _file(folder, "leftover.dat", size=1234)
    sibling = _file(root, "unrelated.bin", size=42)

    cart.add(folder, "Cleanup Recommendations")
    cart.add(inside, "Search & Filter")
    cart.add(sibling, "Main tree")

    effective = cart.resolve_effective_items()

    assert (folder, "Cleanup Recommendations") in effective
    assert (sibling, "Main tree") in effective
    assert not any(node is inside for node, _label in effective)
    assert len(effective) == 2


def test_resolve_effective_items_parent_wins_regardless_of_add_order():
    cart = CartManager()
    root = _root()
    folder = _dir(root, "SomeApp", size=5000)
    inside = _file(folder, "leftover.dat", size=1234)

    # Child added first, parent added second.
    cart.add(inside, "Search & Filter")
    cart.add(folder, "Cleanup Recommendations")

    effective = cart.resolve_effective_items()

    assert len(effective) == 1
    assert effective[0][0] is folder


def test_resolve_effective_items_a_directory_is_not_nested_under_itself():
    cart = CartManager()
    root = _root()
    folder = _dir(root, "SomeApp", size=5000)

    cart.add(folder, "Cleanup Recommendations")

    effective = cart.resolve_effective_items()

    assert effective == [(folder, "Cleanup Recommendations")]


def test_resolve_effective_items_does_not_falsely_match_a_sibling_with_a_shared_prefix():
    """SomeApp and SomeApp2 share a string prefix but are not nested --
    a naive (non-path-aware) prefix check would wrongly drop SomeApp2."""
    cart = CartManager()
    root = _root()
    folder = _dir(root, "SomeApp", size=5000)
    sibling = _dir(root, "SomeApp2", size=999)

    cart.add(folder, "Cleanup Recommendations")
    cart.add(sibling, "Cleanup Recommendations")

    effective = cart.resolve_effective_items()

    assert len(effective) == 2
    assert any(node is sibling for node, _label in effective)


def test_items_preserves_insertion_order():
    cart = CartManager()
    root = _root()
    f1 = _file(root, "a.bin")
    f2 = _file(root, "b.bin")
    f3 = _file(root, "c.bin")

    cart.add(f1, "Main tree")
    cart.add(f2, "Duplicate Files")
    cart.add(f3, "Search & Filter")

    assert [node for node, _label in cart.items()] == [f1, f2, f3]


def test_add_sampled_flag_tracks_sampled_duplicates():
    """CartManager tracks nodes added with is_sampled=True."""
    cart = CartManager()
    root = _root()
    f1 = _file(root, "file1.txt", size=100)
    f2 = _file(root, "file2.txt", size=100)

    cart.add(f1, "Duplicate Files", is_sampled=True)
    cart.add(f2, "Duplicate Files", is_sampled=False)

    assert cart.count_sampled_in_effective_items() == 1


def test_a_sampled_flag_follows_the_file_not_the_view_it_came_through():
    """The main tree and the duplicate window each hold their own FileNode
    for a file; queueing it from both is one entry, flagged as sampled."""
    cart = CartManager()
    root = _root()
    from_main_tree = _file(root, "large.bin", size=5_000_000)
    from_duplicates = FileNode(root, from_main_tree.index)

    cart.add(from_main_tree, "Main tree")
    cart.add(from_duplicates, "Duplicate Files", is_sampled=True)

    assert cart.items() == [(from_main_tree, "Duplicate Files")]
    assert cart.count_sampled_in_effective_items() == 1


def test_add_sampled_then_re_add_unsampled_clears_flag():
    """Re-adding a sampled node without is_sampled clears the flag."""
    cart = CartManager()
    root = _root()
    f = _file(root, "file.txt", size=100)

    cart.add(f, "Main tree", is_sampled=True)
    assert cart.count_sampled_in_effective_items() == 1

    # Re-add without is_sampled flag
    cart.add(f, "Updated source", is_sampled=False)
    assert cart.count_sampled_in_effective_items() == 0


def test_remove_clears_sampled_flag():
    """Removing a node also removes it from the sampled set."""
    cart = CartManager()
    root = _root()
    f = _file(root, "file.txt", size=100)

    cart.add(f, "Duplicate Files", is_sampled=True)
    assert cart.count_sampled_in_effective_items() == 1

    cart.remove(f)
    # After removing, the file isn't even in the cart, so count is 0
    assert len(cart) == 0
    assert cart.count_sampled_in_effective_items() == 0


def test_clear_clears_sampled_flags():
    """Clearing the cart also clears all sampled flags."""
    cart = CartManager()
    root = _root()
    f1 = _file(root, "file1.txt", size=100)
    f2 = _file(root, "file2.txt", size=100)

    cart.add(f1, "Duplicate Files", is_sampled=True)
    cart.add(f2, "Duplicate Files", is_sampled=True)
    assert cart.count_sampled_in_effective_items() == 2

    cart.clear()
    assert len(cart) == 0
    assert cart.count_sampled_in_effective_items() == 0


def test_sampled_file_nested_under_queued_folder_is_not_counted():
    """count_sampled_in_effective_items counts sampled items after de-nesting."""
    cart = CartManager()
    root = _root()
    folder = _dir(root, "Documents", size=5000)
    f = _file(folder, "large.bin", size=100)

    # Add both: the folder (which contains the file) and the file itself (sampled)
    cart.add(folder, "Main tree", is_sampled=False)
    cart.add(f, "Duplicate Files", is_sampled=True)

    # After resolving effective items, the folder wins (parent takes precedence)
    # So the file is dropped from effective items, and the count should be 0
    # because the effective folder itself is not sampled
    assert cart.count_sampled_in_effective_items() == 0
