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