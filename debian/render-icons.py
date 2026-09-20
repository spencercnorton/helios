"""render-icons.py <master.png> <hicolor-root> <icon-name>: every standard hicolor size."""
import sys
from pathlib import Path

from PIL import Image

master, root, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
img = Image.open(master).convert("RGBA")
for size in (16, 24, 32, 48, 64, 128, 256, 512):
    out = root / f"{size}x{size}" / "apps"
    out.mkdir(parents=True, exist_ok=True)
    img.resize((size, size), Image.LANCZOS).save(out / f"{name}.png", "PNG", optimize=True)
