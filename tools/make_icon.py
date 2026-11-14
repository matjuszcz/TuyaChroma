"""Generate the application icon (green ring, like the tray icon)."""

import sys

from PIL import Image, ImageDraw

size = 256
img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
draw.ellipse((16, 16, size - 16, size - 16), fill="#44d62c")
draw.ellipse((88, 88, size - 88, size - 88), fill=(255, 255, 255, 255))
img.save(sys.argv[1] if len(sys.argv) > 1 else "icon.ico",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
