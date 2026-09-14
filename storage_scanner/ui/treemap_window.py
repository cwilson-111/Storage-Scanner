"""Interactive treemap explorer.

Rectangle area = size, color = heat (same green→amber→red scale used
elsewhere in the app). Click a folder to drill into it, use the breadcrumb
to jump back up, double-click a file/folder to reveal it, hover for exact
size/count. Only one level is drawn at a time — this is a drill-down
explorer, not a single all-levels-at-once view.

A mixin composed into StorageScannerApp (storage_scanner/app.py).
"""

from tkinter import BOTH, Canvas, LEFT, TOP, Toplevel, X, ttk

from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import resource_path
from storage_scanner.settings import COLORS, heat_color
from storage_scanner.treemap import compute_layout

_MIN_LABEL_W = 42
_MIN_LABEL_H = 16


class TreemapMixin:
    def show_treemap(self):
        if not self.root_node:
            return

        existing = getattr(self, "_treemap_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._treemap_win = win
        win.configure(bg=COLORS["bg"])
        win.title("Treemap")
        win.geometry("900x620")
        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Treemap window iconbitmap failed", exc_info=True)

        stack = [self.root_node]

        nav_bar = ttk.Frame(win, padding=(10, 8, 10, 4))
        nav_bar.pack(side=TOP, fill=X)

        canvas = Canvas(win, bg=COLORS["bg"], highlightthickness=0, bd=0)
        canvas.pack(fill=BOTH, expand=True, padx=10, pady=(0, 4))

        info_label = ttk.Label(win, padding=(10, 0, 10, 8), style="Accent.TLabel")
        info_label.pack(side=TOP, fill=X)

        tooltip_ids = []

        def hide_tooltip():
            for item_id in tooltip_ids:
                canvas.delete(item_id)
            tooltip_ids.clear()

        def show_tooltip(event, node):
            hide_tooltip()
            text = f"{node.name}\n{human_size(node.size)}"
            if node.is_dir:
                text += f"\n{node.file_count:,} files"
            text_id = canvas.create_text(
                event.x + 14, event.y + 14, anchor="nw", text=text,
                fill=COLORS["bg"], font=("Segoe UI", 9),
            )
            bbox = canvas.bbox(text_id)
            bg_id = None
            if bbox:
                bg_id = canvas.create_rectangle(
                    bbox[0] - 5, bbox[1] - 3, bbox[2] + 5, bbox[3] + 3,
                    fill=COLORS["accent2"], outline="",
                )
                canvas.tag_lower(bg_id, text_id)
            tooltip_ids.extend(i for i in (bg_id, text_id) if i is not None)

        def redraw_breadcrumb():
            for child in nav_bar.winfo_children():
                child.destroy()
            for index, node in enumerate(stack):
                label = node.name if len(node.name) <= 22 else node.name[:19] + "…"
                ttk.Button(
                    nav_bar, text=label, command=lambda i=index: navigate_to(i),
                ).pack(side=LEFT)
                if index < len(stack) - 1:
                    ttk.Label(nav_bar, text=" › ").pack(side=LEFT)

        def redraw_canvas(_event=None):
            hide_tooltip()
            canvas.delete("all")
            node = stack[-1]

            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if w <= 2 or h <= 2:
                return

            children = [c for c in node.children if c.size > 0]
            if not children:
                canvas.create_text(
                    w / 2, h / 2, text="(empty)",
                    fill=COLORS["muted"], font=("Segoe UI", 11),
                )
            else:
                layout = compute_layout([(c, c.size) for c in children], 0, 0, w, h)
                max_size = max(c.size for c in children) or 1

                for child, rx, ry, rw, rh in layout:
                    fraction = child.size / max_size
                    fill = heat_color(fraction)
                    rect_id = canvas.create_rectangle(
                        rx, ry, rx + rw, ry + rh, fill=fill, outline=COLORS["bg"],
                    )
                    canvas.tag_bind(rect_id, "<Button-1>", lambda e, n=child: on_click(n))
                    canvas.tag_bind(rect_id, "<Double-1>", lambda e, n=child: on_double(n))
                    canvas.tag_bind(rect_id, "<Motion>", lambda e, n=child: show_tooltip(e, n))
                    canvas.tag_bind(rect_id, "<Leave>", lambda _e: hide_tooltip())

                    if rw >= _MIN_LABEL_W and rh >= _MIN_LABEL_H:
                        label = child.name if len(child.name) <= 22 else child.name[:19] + "…"
                        text_color = "#05070c" if fraction > 0.5 else COLORS["fg"]
                        canvas.create_text(
                            rx + 4, ry + 3, anchor="nw", text=label,
                            fill=text_color, font=("Segoe UI", 9),
                        )
                        if rh >= _MIN_LABEL_H * 2:
                            canvas.create_text(
                                rx + 4, ry + 17, anchor="nw", text=human_size(child.size),
                                fill=text_color, font=("Segoe UI", 8),
                            )

            info_label.config(
                text=f"{node.path}  —  {human_size(node.size)} in {node.file_count:,} files"
                     f"  ({len(children)} item(s) shown at this level)"
            )

        def navigate_to(index):
            del stack[index + 1:]
            redraw_breadcrumb()
            redraw_canvas()

        def on_click(node):
            if node.is_dir and node.children:
                stack.append(node)
                redraw_breadcrumb()
                redraw_canvas()

        def on_double(node):
            self._reveal(node.path, is_dir=node.is_dir)

        canvas.bind("<Configure>", redraw_canvas)
        redraw_breadcrumb()
