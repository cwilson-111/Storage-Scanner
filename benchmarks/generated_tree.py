"""The folder tree benchmarks/scan.py scans, and the check that a scan of
it is correct.

generate_tree() builds a tree from a TreeSpec and a seed (the same seed and
spec always produce the same files, names, and sizes) and returns a Manifest
of what a correct scan must report; verify() lists every way a scan differs
from that.

The tree covers the edge cases the scanner makes promises about: a random
nested tree, empty folders, a deep folder chain, hard links (each counted
once), a symlink or junction to a folder with files in it (never followed),
and unusual names (Unicode, spaces, leading dots, a 100-character name). A
link the OS won't let this account create is skipped, and the manifest
records which ones were created. File contents are zeros; only the metadata
matters to a scan.

Standard library only.
"""

import os
import random
import shutil
import sys
from dataclasses import dataclass, field

_IS_WINDOWS = sys.platform == "win32"

_EXTENSIONS = (".txt", ".log", ".jpg", ".png", ".pdf", ".docx", ".zip", ".dll", ".json", "")
_ODD_NAMES = (
    "ünïcødé ファイル.txt",
    "name with  spaces.txt",
    ".hidden-file",
    "no_extension",
    "many.dots.in.the.name.tar.gz",
    # 100 characters: long for a single name, yet its full path still fits in
    # Windows' 260-character MAX_PATH (long-path support is off by default)
    # from any reasonably placed folder, pytest's deep tmp_path included.
    "x" * 96 + ".bin",
)


@dataclass(frozen=True)
class TreeSpec:
    dirs: int  # folders in the random nested tree
    files: int  # files spread across those folders
    hardlinks: int  # files that each get one extra hard link
    chain_depth: int  # folders in the single deep chain


PROFILES = {
    "small": TreeSpec(dirs=200, files=2_000, hardlinks=20, chain_depth=40),
    "medium": TreeSpec(dirs=2_000, files=20_000, hardlinks=100, chain_depth=60),
    "large": TreeSpec(dirs=10_000, files=100_000, hardlinks=200, chain_depth=60),
}


@dataclass
class Manifest:
    """What a correct scan of the generated tree must report.

    `top_level` maps each folder directly under the root to its expected
    [size, file_count]. Hard links and their originals sit under the same
    top-level folder, so those totals don't depend on which occurrence a
    scan happens to count first.
    """

    root: str
    top_level: "dict[str, list[int]]" = field(default_factory=dict)
    dir_count: int = 0  # folders below the root
    hardlink_extras: int = 0  # extra hard-link entries actually created
    links: "list[str]" = field(default_factory=list)  # symlinks/junctions created
    link_kinds: "list[str]" = field(default_factory=list)

    @property
    def total_size(self):
        return sum(size for size, _files in self.top_level.values())

    @property
    def total_files(self):
        return sum(files for _size, files in self.top_level.values())


def _file_size(rng):
    """Mostly small files with a long tail, so a large profile stays around
    a gigabyte on disk instead of tens of them."""
    roll = rng.random()
    if roll < 0.90:
        return rng.randint(0, 4096)
    if roll < 0.999:
        return rng.randint(4097, 64 * 1024)
    return rng.randint(64 * 1024 + 1, 8 * 1024 * 1024)


def _write_file(path, size):
    with open(path, "wb") as f:
        if size:
            f.seek(size - 1)
            f.write(b"\0")


def _mkdir(path, manifest):
    os.mkdir(path)
    manifest.dir_count += 1


def _add_files(manifest, top, entries):
    """Write (path, size) entries and count them under top-level folder `top`."""
    totals = manifest.top_level.setdefault(top, [0, 0])
    for path, size in entries:
        _write_file(path, size)
        totals[0] += size
        totals[1] += 1


def _gen_random_tree(base, spec, rng, manifest):
    _mkdir(base, manifest)
    dirs = [base]
    for i in range(spec.dirs):
        parent = rng.choice(dirs)
        path = os.path.join(parent, f"dir{i:05d}")
        _mkdir(path, manifest)
        dirs.append(path)
    # Folders the loop below never picks stay empty, which is realistic too.
    _add_files(
        manifest,
        "tree",
        (
            (os.path.join(rng.choice(dirs), f"f{i:06d}{rng.choice(_EXTENSIONS)}"), _file_size(rng))
            for i in range(spec.files)
        ),
    )


def _gen_chain(base, spec, rng, manifest):
    # One-letter names keep the chain under Windows' 260-character MAX_PATH
    # from a normal temp folder; the depth, not the length, is the point.
    path = base
    _mkdir(path, manifest)
    for _ in range(spec.chain_depth):
        path = os.path.join(path, "d")
        _mkdir(path, manifest)
    _add_files(manifest, "chain", [(os.path.join(path, "bottom.bin"), _file_size(rng))])


def _gen_hardlinks(base, spec, rng, manifest):
    originals_dir = os.path.join(base, "originals")
    links_dir = os.path.join(base, "links")
    for path in (base, originals_dir, links_dir):
        _mkdir(path, manifest)
    originals = [
        (os.path.join(originals_dir, f"h{i:04d}.bin"), _file_size(rng))
        for i in range(spec.hardlinks)
    ]
    _add_files(manifest, "hardlinks", originals)
    totals = manifest.top_level["hardlinks"]
    for i, (original, _size) in enumerate(originals):
        try:
            os.link(original, os.path.join(links_dir, f"h{i:04d}.bin"))
        except OSError:  # e.g. FAT/exFAT, or a filesystem without hard links
            break
        totals[1] += 1  # an extra entry, but its bytes are only counted once
        manifest.hardlink_extras += 1


def _gen_names(base, rng, manifest):
    _mkdir(base, manifest)
    _add_files(
        manifest, "names", [(os.path.join(base, name), _file_size(rng)) for name in _ODD_NAMES]
    )
    _mkdir(os.path.join(base, "empty"), manifest)


def _try_link(manifest, kind, create, path):
    """Create one symlink/junction; a scan must record it as a single leaf
    entry with its own lstat() size, never follow it."""
    try:
        create()
    except (OSError, NotImplementedError):
        return  # no symlink privilege (Windows without Developer Mode), etc.
    totals = manifest.top_level["links"]
    totals[0] += os.lstat(path).st_size
    totals[1] += 1
    manifest.links.append(path)
    manifest.link_kinds.append(kind)


def _gen_links(base, manifest):
    target = os.path.join(base, "target")
    for path in (base, target):
        _mkdir(path, manifest)
    # Enough bytes behind the links that following one would be obvious.
    _add_files(
        manifest, "links", [(os.path.join(target, f"t{i}.bin"), 1024 * (i + 1)) for i in range(5)]
    )

    # Relative targets, so a POSIX symlink's lstat size (its target's length)
    # doesn't depend on where the tree was generated.
    dir_link = os.path.join(base, "dir-symlink")
    _try_link(
        manifest,
        "dir-symlink",
        lambda: os.symlink("target", dir_link, target_is_directory=True),
        dir_link,
    )
    file_link = os.path.join(base, "file-symlink")
    _try_link(
        manifest,
        "file-symlink",
        lambda: os.symlink(os.path.join("target", "t0.bin"), file_link),
        file_link,
    )
    if _IS_WINDOWS:
        import _winapi  # CPython's own junction helper; needs no privilege

        junction = os.path.join(base, "junction")
        _try_link(manifest, "junction", lambda: _winapi.CreateJunction(target, junction), junction)


def generate_tree(root, spec, seed):
    """Create the tree for (spec, seed) under the new folder `root` and
    return its Manifest. `root` must not already exist."""
    rng = random.Random(seed)
    os.mkdir(root)
    manifest = Manifest(root=root)
    try:
        _gen_random_tree(os.path.join(root, "tree"), spec, rng, manifest)
        _gen_chain(os.path.join(root, "chain"), spec, rng, manifest)
        _gen_hardlinks(os.path.join(root, "hardlinks"), spec, rng, manifest)
        _gen_names(os.path.join(root, "names"), rng, manifest)
        _gen_links(os.path.join(root, "links"), manifest)
    except BaseException:
        remove_tree(manifest)  # don't leave a half-built tree behind
        raise
    return manifest


def remove_tree(manifest):
    """Delete a generated tree, removing its links first so nothing ever
    deletes through one into its target."""
    for path, kind in zip(manifest.links, manifest.link_kinds):
        if _IS_WINDOWS and kind in ("dir-symlink", "junction"):
            os.rmdir(path)  # removes the link itself, not the folder it points to
        else:
            os.unlink(path)
    shutil.rmtree(manifest.root)


def _walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.children)


def verify(root_node, manifest):
    """Every way the scan differs from the generated tree, as readable
    strings; an empty list means it matched exactly."""
    problems = []

    def check(label, scanned, expected):
        if scanned != expected:
            problems.append(f"{label}: scanned {scanned}, expected {expected}")

    check("total size", root_node.size, manifest.total_size)
    check("file count", root_node.file_count, manifest.total_files)
    nodes = list(_walk(root_node))
    check("folder count", sum(n.is_dir for n in nodes) - 1, manifest.dir_count)
    check("hard-link duplicates", sum(n.hardlink_dup for n in nodes), manifest.hardlink_extras)
    check("unreadable entries", sum(n.error for n in nodes), 0)

    children = {child.name: child for child in root_node.children}
    for name, (size, files) in sorted(manifest.top_level.items()):
        node = children.get(name)
        if node is None:
            problems.append(f"{name}: missing from the scan")
            continue
        check(f"{name}/ size", node.size, size)
        check(f"{name}/ file count", node.file_count, files)
    problems.extend(
        f"{name}: in the scan but never generated"
        for name in sorted(set(children) - set(manifest.top_level))
    )

    by_path = {os.path.normcase(n.path): n for n in nodes}
    for path in manifest.links:
        node = by_path.get(os.path.normcase(path))
        if node is None:
            problems.append(f"{path}: link missing from the scan")
        elif node.is_dir or node.children:
            problems.append(f"{path}: link was followed instead of recorded as a leaf")
    return problems
