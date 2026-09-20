from pathlib import Path
from PIL import Image

BASE_DIR = Path(__file__).parent

img = Image.open(BASE_DIR / "docs" / "Designer.png")

img.save(
    BASE_DIR / "icon.ico",
    format="ICO",
    sizes=[
        (16, 16),
        (32, 32),
        (48, 48),
        (64, 64),
        (128, 128),
        (256, 256)
    ]
)
print("Wrote icon.ico")

# For the macOS .app bundle's own Finder icon (PyInstaller's --icon on
# macOS wants .icns, not .ico). Pillow's ICNS writer needs no macOS-only
# tool (no iconutil) -- it builds the whole multi-resolution bundle
# itself, so this runs the same way on any build platform.
img.save(BASE_DIR / "icon.icns", format="ICNS")
print("Wrote icon.icns")