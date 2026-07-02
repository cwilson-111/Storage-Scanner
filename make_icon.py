#!/usr/bin/env python3
"""
Generate the TreeSize app icon: a stylized binary search tree.

Draws at high resolution then exports a multi-size Windows .ico
(16/32/48/64/128/256). Run once to (re)create icon.ico:

    python make_icon.py
"""

from PIL import Image, ImageDraw

# High-res master canvas; the .ico is downscaled from this for crispness.
S = 1024
BG_TOP = (37, 99, 235)       # blue gradient top
BG_BOT = (29, 78, 216)       # blue gradient bottom
EDGE = (191, 219, 254)       # light blue branches
NODE = (255, 255, 255)       # white nodes
NODE_RING = (37, 99, 235)    # blue ring around nodes

# Binary search tree layout: (id, x, y) as fractions of the canvas.
NODES = {
    "root": (0.50, 0.18),
    "l":    (0.28, 0.42),
    "r":    (0.72, 0.42),
    "ll":   (0.16, 0.68),
    "lr":   (0.40, 0.68),
    "rr":   (0.84, 0.68),
    "lll":  (0.10, 0.90),
    "lrr":  (0.46, 0.90),
}
EDGES = [
    ("root", "l"), ("root", "r"),
    ("l", "ll"), ("l", "lr"),
    ("r", "rr"),
    ("ll", "lll"), ("lr", "lrr"),
]
NODE_R = 0.052  # node radius as fraction of canvas


def rounded_gradient(size, radius):
    """A vertical blue gradient clipped to a rounded square."""
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    for y in range(size):
        t = y / (size - 1)
        r = int(BG_TOP[0] + (BG_BOT[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOT[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOT[2] - BG_TOP[2]) * t)
        for x in range(size):
            grad.putpixel((x, y), (r, g, b, 255))
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    base.paste(grad, (0, 0), mask)
    return base


def draw_tree():
    img = rounded_gradient(S, radius=int(S * 0.22))
    d = ImageDraw.Draw(img)

    def px(node):
        fx, fy = NODES[node]
        return fx * S, fy * S

    # Branches first, so nodes sit on top.
    line_w = int(S * 0.018)
    for a, b in EDGES:
        d.line([px(a), px(b)], fill=EDGE, width=line_w)

    # Nodes: white disc with a blue ring.
    r = NODE_R * S
    ring = int(S * 0.012)
    for node in NODES:
        cx, cy = px(node)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=NODE_RING)
        d.ellipse(
            [cx - r + ring, cy - r + ring, cx + r - ring, cy + r - ring],
            fill=NODE,
        )
    return img


def main():
    master = draw_tree()
    sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    master.save("icon.ico", sizes=sizes)
    master.resize((256, 256), Image.LANCZOS).save("docs/icon_preview.png")
    print("Wrote icon.ico and docs/icon_preview.png")


if __name__ == "__main__":
    main()
