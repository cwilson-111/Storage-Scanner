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
