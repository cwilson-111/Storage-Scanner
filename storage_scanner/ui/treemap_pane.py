"""The treemap under the main tree: the selected folder as nested tiles,
each tile's area its size on disk (storage_scanner/treemap_model.py),
shaded as cushions (storage_scanner/treemap_cushion.py) unless Tools ▸
Explore ▸ Shade Treemap Tiles is off, when they're flat outlined boxes.

It follows the tree: selecting a folder shows it, and selecting something
inside the folder already shown only highlights its tile. The other way
round, clicking a tile selects its row in the tree (opening folders and
loading pages on the way), double-clicking a folder shows it, and
double-clicking a file reveals it; right-click opens the tree's own menu
for it. Clicks are hit-tested on the whole canvas, so a label is as
clickable as the tile under it, and only the deepest tile at the pointer
answers.
"""

from tkinter import BOTH, BOTTOM, LEFT, RIGHT, TOP, Canvas, PhotoImage, StringVar, X, ttk
from tkinter import font as tkfont

from storage_scanner.formatting import human_size
from storage_scanner.live_tree_model import date_text
from storage_scanner.platform_support import IS_MACOS
from storage_scanner.settings import COLORS, FONT, contrast_text_color, px
from storage_scanner.treemap_cushion import render, shade
from storage_scanner.treemap_model import (
    MODES,
    SIZE,
    area_of,
    chain,
    hit_test,
    nested_tiles,
    tile_color,
)

_REDRAW_DELAY_MS = 60  # resizes and selection changes come in bursts


class TreemapPane:
    """The pane; `frame` is what the main window places."""

    def __init__(self, app, master):
        self.app = app
        self.frame = ttk.Frame(master)
        self.chain = []  # nodes from the scan's root down to the folder shown
        self.tiles = []
        self.image = None  # the shaded tiles; the canvas only holds its name
        self.highlight = None  # the node whose tile is outlined
        self._pending = None

        bar = ttk.Frame(self.frame, padding=(8, 4, 8, 2))
        bar.pack(side=TOP, fill=X)
        self.up_button = ttk.Button(bar, text="↑ Up", command=self.go_up, state="disabled")
        self.up_button.pack(side=LEFT)
        self.title_var = StringVar(master=master)
        ttk.Label(bar, textvariable=self.title_var, style="Accent.TLabel").pack(
            side=LEFT, padx=(8, 0), fill=X, expand=True
        )
        labels = [label for _mode, label in MODES]
        self.mode_var = StringVar(master=master, value=dict(MODES)[SIZE])
        mode_box = ttk.Combobox(
            bar, textvariable=self.mode_var, values=labels, state="readonly", width=14
        )
        mode_box.pack(side=RIGHT)
        mode_box.bind("<<ComboboxSelected>>", lambda _e: self.schedule())
        ttk.Label(bar, text="Colour by").pack(side=RIGHT, padx=(0, 6))

        self.info_var = StringVar(master=master)
        ttk.Label(self.frame, textvariable=self.info_var, padding=(8, 0, 8, 4)).pack(
            side=BOTTOM, fill=X
        )
        self.canvas = Canvas(
            self.frame,
            bg=COLORS["panel"],
            height=px(220),
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(side=TOP, fill=BOTH, expand=True, padx=8)
        self.font = tkfont.Font(root=master, font=FONT)
        self.char_px = max(1, self.font.measure("0"))
        self.line_px = self.font.metrics("linespace")  # a Tk call: once, not per label

        canvas = self.canvas
        canvas.bind("<Configure>", lambda _e: self.schedule())
        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Leave>", lambda _e: self.info_var.set(""))
        canvas.bind("<Button-1>", self._on_click)
        canvas.bind("<Double-1>", self._on_double)
        canvas.bind("<Button-2>" if IS_MACOS else "<Button-3>", self._on_menu)

    # -- what's shown -------------------------------------------------------- #

    def show(self, nodes, highlight=None):
        """Show the folder at the end of `nodes` (a path of nodes from the
        scan's root down), outlining `highlight` if it's in there."""
        self.chain = list(nodes)
        self.highlight = highlight
        self.up_button.state(["!disabled"] if len(self.chain) > 1 else ["disabled"])
        self.schedule()

    def follow(self, nodes):
        """The tree selected the node at the end of `nodes`: highlight it if
        it's inside the folder shown, else show it (or its folder)."""
        if not nodes:
            return
        node = nodes[-1]
        inside = len(nodes) > len(self.chain) and all(a is b for a, b in zip(self.chain, nodes))
        if inside:
            self.highlight = node
            self.schedule()
        elif node.is_dir:
            self.show(nodes)
        else:
            self.show(nodes[:-1], highlight=node)

    def clear(self):
        self.chain = []
        self.tiles = []
        self.highlight = None
        self.up_button.state(["disabled"])
        self.schedule()

    def refresh(self):
        """Redraw after a delete or new comparison data. A shown folder that
        was deleted is replaced by the nearest folder above it that wasn't."""
        for depth in range(1, len(self.chain)):
            if self.chain[depth] not in self.chain[depth - 1].dirs:
                self.show(self.chain[:depth])
                return
        self.schedule()

    def go_up(self):
        if len(self.chain) > 1:
            folder = self.chain[-1]
            self.show(self.chain[:-1], highlight=folder)
            self.app._select_in_tree(self.chain + [folder])

    def schedule(self):
        if self._pending is not None:
            self.canvas.after_cancel(self._pending)
        self._pending = self.canvas.after(_REDRAW_DELAY_MS, self.redraw)

    # -- drawing --------------------------------------------------------------- #

    def mode(self):
        return next((mode for mode, label in MODES if label == self.mode_var.get()), SIZE)

    def redraw(self):
        self._pending = None
        canvas = self.canvas
        canvas.delete("all")
        self.image = None
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if not self.chain or width < px(20) or height < px(20):
            self.tiles = []
            self.title_var.set("")
            if not self.chain:
                canvas.create_text(
                    width / 2,
                    height / 2,
                    text="The treemap shows the scanned folder here once a scan finishes.",
                    fill=COLORS["muted"],
                    font=FONT,
                )
            return
        folder = self.chain[-1]
        self.title_var.set(f"{folder.path}  —  {human_size(folder.alloc_size)} on disk")
        self.tiles = nested_tiles(folder, width, height, header=px(16), min_px=px(4))
        mode = self.mode()
        app = self.app
        change_of = app._folder_change if app._previous_folder_sizes else None
        fills = [tile_color(tile, mode, change_of) for tile in self.tiles]
        shaded = app.shade_treemap_var.get()
        if shaded and self.tiles:
            image = render(self.tiles, fills, width, height, COLORS["panel"])
            self.image = PhotoImage(master=canvas, data=image, format="PPM")
            canvas.create_image(0, 0, anchor="nw", image=self.image)
        outlined = None
        for index, (tile, fill) in enumerate(zip(self.tiles, fills)):
            if not shaded:
                canvas.create_rectangle(
                    tile.x,
                    tile.y,
                    tile.x + tile.w,
                    tile.y + tile.h,
                    fill=fill,
                    outline=COLORS["panel"],
                )
            self._label(index, fill, shaded)
            if self.highlight is not None and tile.node == self.highlight:
                outlined = tile
        if not self.tiles:
            canvas.create_text(
                width / 2, height / 2, text="(empty)", fill=COLORS["muted"], font=FONT
            )
        if outlined is not None:
            canvas.create_rectangle(
                outlined.x + 1,
                outlined.y + 1,
                outlined.x + outlined.w - 1,
                outlined.y + outlined.h - 1,
                outline=COLORS["accent"],
                width=px(3),
            )

    def _label(self, index, fill, shaded):
        tile = self.tiles[index]
        line = self.line_px
        if tile.w < px(36) or tile.h < line:
            return
        if tile.node is None:
            text = f"… {tile.rest_count:,} more"
        else:
            text = tile.node.name
            if not tile.node.is_dir and tile.h >= 2 * line + px(4):
                text += f"\n{human_size(area_of(tile.node))}"
        fit = max(1, int((tile.w - px(6)) // self.char_px))
        lines = [
            part if len(part) <= fit else part[: max(1, fit - 1)] + "…" for part in text.split("\n")
        ]
        x, y = tile.x + px(3), tile.y + px(1)
        if shaded:  # what's under the middle of the first line
            middle = x + len(lines[0]) * self.char_px / 2, y + line / 2
            fill = shade(self.tiles, index, fill, *middle)
        self.canvas.create_text(
            x,
            y,
            anchor="nw",
            text="\n".join(lines),
            fill=contrast_text_color(fill),
            font=FONT,
        )

    # -- pointer --------------------------------------------------------------- #

    def _tile_at(self, event):
        index = hit_test(self.tiles, event.x, event.y)
        if index is None or self.tiles[index].node is None:
            return None, None
        return index, self.chain + chain(self.tiles, index)

    def _on_motion(self, event):
        index = hit_test(self.tiles, event.x, event.y)
        if index is None:
            self.info_var.set("")
            return
        tile = self.tiles[index]
        if tile.node is None:
            self.info_var.set(
                f"{tile.rest_count:,} smaller items, {human_size(tile.rest_bytes)} on disk"
            )
            return
        node = tile.node
        details = f"{node.path}  —  {human_size(area_of(node))} on disk"
        if node.is_dir:
            details += f", {node.file_count:,} files"
        if node.mtime:
            details += f", modified {date_text(node.mtime)}"
        self.info_var.set(details)

    def _on_click(self, event):
        index, nodes = self._tile_at(event)
        if nodes:
            self.highlight = nodes[-1]
            self.app._select_in_tree(nodes)
            self.schedule()

    def _on_double(self, event):
        index, nodes = self._tile_at(event)
        if not nodes:
            return
        node = nodes[-1]
        if node.is_dir:
            self.show(nodes)
            self.app._select_in_tree(nodes)
        else:
            self.app._reveal(node.path, is_dir=False)

    def _on_menu(self, event):
        index, nodes = self._tile_at(event)
        if nodes:
            self.highlight = nodes[-1]
            self.app._select_in_tree(nodes)
            self.schedule()
            self.app.menu.tk_popup(event.x_root, event.y_root)
