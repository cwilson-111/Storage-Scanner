"""The Cleanup Cart window: review and execute a cross-window delete queue.

A mixin composed into StorageScannerApp (storage_scanner/app.py). The
queue itself (storage_scanner.cart.CartManager) is pure logic; this module
is just the Toplevel window and the batch-delete executor built on top of
it, mirroring the cleanup_recommendations.py / cleanup_window.py split.
"""

from tkinter import (
    BOTH,
    BOTTOM,
    END,
    LEFT,
    RIGHT,
    TOP,
    E,
    StringVar,
    Toplevel,
    W,
    X,
    messagebox,
    ttk,
)

from storage_scanner import audit
from storage_scanner.audit import recycle_and_log
from storage_scanner.formatting import human_size
from storage_scanner.logging_setup import logger
from storage_scanner.platform_support import FILE_MANAGER_NAME, TRASH_NAME, resource_path
from storage_scanner.settings import COLORS


def _cart_failure_reason(node):
    """A specific reason a cart item couldn't be deleted, when one is
    knowable, falling back to the same generic message every other
    delete-confirmation dialog in the app already shows.

    Cart items can sit queued for a long time before "Execute Deletions"
    runs, which makes audit.check_stale's TOCTOU refusal far more likely
    to fire here than anywhere else — worth naming specifically rather
    than lumping it into "could not delete" like a permissions/in-use
    failure.
    """
    stale_message = audit.check_stale(node)
    if stale_message is not None:
        return stale_message
    return "Could not delete — it may be in use, protected, or require admin rights."


class CartMixin:
    def _refresh_cart_indicator(self):
        """Update the toolbar's cart button text and the cart window's own
        header/title, if either currently exists. Called after every
        add/remove/clear/execute so neither ever shows a stale count."""
        count = len(self.cart)
        total = self.cart.total_bytes()

        cart_btn = getattr(self, "cart_btn", None)
        if cart_btn is not None:
            text = f"🛒 Cart ({count:,}) — {human_size(total)}" if count else "🛒 Cart"
            cart_btn.config(text=text)

        win = getattr(self, "_cart_win", None)
        refresh = getattr(self, "_cart_win_refresh", None)
        if win is not None and win.winfo_exists() and refresh is not None:
            refresh()

    def show_cart(self):
        existing = getattr(self, "_cart_win", None)
        if existing is not None and existing.winfo_exists():
            existing.destroy()

        win = Toplevel(self.root)
        self._cart_win = win
        win.configure(bg=COLORS["bg"])
        win.geometry("900x560")

        try:
            win.iconbitmap(resource_path("icon.ico"))
        except Exception:  # noqa: BLE001
            logger.debug("Cart window iconbitmap failed", exc_info=True)

        header_var = StringVar()

        ttk.Label(
            win,
            textvariable=header_var,
            padding=(10, 8),
            style="Accent.TLabel",
        ).pack(side=TOP, fill=X)

        frame = ttk.Frame(win, padding=(10, 0, 10, 10))
        frame.pack(fill=BOTH, expand=True)

        cols = ("source", "kind", "size", "path")
        tv = ttk.Treeview(frame, columns=cols, show="headings", selectmode="extended")
        tv.heading("source", text="Added from")
        tv.heading("kind", text="Type")
        tv.heading("size", text="Size")
        tv.heading("path", text="Path")
        tv.column("source", width=170, anchor=W, stretch=False)
        tv.column("kind", width=60, anchor=W, stretch=False)
        tv.column("size", width=100, anchor=E, stretch=False)
        tv.column("path", width=500, anchor=W, stretch=True)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        tv.tag_configure("even", background=COLORS["panel"])
        tv.tag_configure("odd", background=COLORS["stripe"])

        iid_to_node = {}

        def populate():
            tv.delete(*tv.get_children())
            iid_to_node.clear()
            for index, (node, source_label) in enumerate(self.cart.items()):
                iid = tv.insert(
                    "",
                    END,
                    values=(
                        source_label,
                        "Folder" if node.is_dir else "File",
                        human_size(node.size),
                        node.path,
                    ),
                    tags=("odd" if index % 2 else "even",),
                )
                iid_to_node[iid] = node

            count = len(self.cart)
            total = self.cart.total_bytes()
            header_var.set(f"{count:,} item(s) in cart — {human_size(total)} reclaimable")
            win.title(f"Cleanup Cart — {count} item(s)")

        self._cart_win_refresh = populate
        populate()

        def reveal_selected():
            sel = tv.focus()
            node = iid_to_node.get(sel)
            if node:
                self._reveal(node.path, is_dir=node.is_dir)

        def remove_selected():
            selected = list(tv.selection())
            for iid in selected:
                node = iid_to_node.get(iid)
                if node:
                    self.cart.remove(node)
            populate()
            self._refresh_cart_indicator()

        def clear_cart():
            if not self.cart:
                return
            if not messagebox.askyesno(
                "Clear Cart",
                "Remove every item from the cart? Nothing is deleted.",
                parent=win,
            ):
                return
            self.cart.clear()
            populate()
            self._refresh_cart_indicator()

        def execute_deletions():
            if self._refuse_delete_during_scan(parent=win):
                return
            effective = self.cart.resolve_effective_items()
            if not effective:
                messagebox.showinfo("Storage Scanner", "Cart is empty.", parent=win)
                return

            total = sum(node.size for node, _label in effective)

            # Check for sampled duplicates in the cart
            sampled_count = self.cart.count_sampled_in_effective_items()
            sampled_warning = (
                (
                    f"\n\n⚠ {sampled_count} item(s) are from sampled duplicate matches "
                    "(only first, middle, and last 1 MB compared — bytes between "
                    "the compared windows weren't checked)."
                )
                if sampled_count
                else ""
            )

            if not messagebox.askyesno(
                f"Delete to {TRASH_NAME}",
                f"Send {len(effective):,} item(s) ({human_size(total)}) "
                f"to the {TRASH_NAME}?{sampled_warning}",
                icon="warning",
                parent=win,
            ):
                return
            # Built once per batch: which cart nodes still have a live row
            # in the main tree (main_window._remove_main_tree_row's ancestor
            # rollup/refresh only applies there) vs. only in the scanned
            # model (search_window._remove_search_result_from_tree).
            iid_for_node = {v: k for k, v in self.node_by_iid.items()}

            deleted = 0
            failures = []  # (path, reason)

            for node, source_label in effective:
                if recycle_and_log(node, source=f"Cleanup Cart ({source_label})"):
                    deleted += 1
                    self._remove_from_duplicate_cache(node)
                    main_iid = iid_for_node.get(node)
                    if main_iid is not None and self.tree.exists(main_iid):
                        self._remove_main_tree_row(main_iid)
                    else:
                        self._remove_search_result_from_tree(node)
                else:
                    failures.append((node.path, _cart_failure_reason(node)))
                self.cart.remove(node)

            populate()
            self._refresh_cart_indicator()
            self.status_var.set(f"Deleted {deleted:,} item(s) to {TRASH_NAME}.")

            if failures:
                detail = "\n\n".join(f"{path}\n  {reason}" for path, reason in failures[:10])
                messagebox.showerror(
                    "Storage Scanner",
                    "Some items could not be deleted:\n\n" + detail,
                    parent=win,
                )

        button_bar = ttk.Frame(win, padding=(10, 0, 10, 10))
        button_bar.pack(side=BOTTOM, fill=X)

        ttk.Button(
            button_bar,
            text=f"Reveal in {FILE_MANAGER_NAME}",
            command=reveal_selected,
        ).pack(side=LEFT)
        ttk.Button(
            button_bar,
            text="Remove Selected",
            command=remove_selected,
        ).pack(side=LEFT, padx=6)
        ttk.Button(button_bar, text="Clear Cart", command=clear_cart).pack(side=LEFT)
        ttk.Button(
            button_bar,
            text="Execute Deletions",
            command=execute_deletions,
        ).pack(side=RIGHT)

        tv.bind("<Double-1>", lambda _e: reveal_selected())
