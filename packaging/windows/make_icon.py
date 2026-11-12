"""Draws the app's icon (a spectrum peak over waterfall lines, like the Android app's) as
build/icon.ico at every size Windows uses. Run from the repository's root."""

from pathlib import Path

from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=48, fill=(14, 26, 36, 255))
peak = [(40, 150), (70, 146), (88, 148), (100, 96), (128, 62), (156, 96), (168, 148), (186, 144), (216, 150)]
d.line(peak, fill=(79, 209, 197, 255), width=12, joint="curve")
for y, x0, x1, colour in ((176, 48, 208, (43, 108, 176)), (198, 64, 192, (49, 130, 206)), (220, 88, 168, (99, 179, 237))):
    d.line([(x0, y), (x1, y)], fill=colour + (255,), width=11)
out = Path("build")
out.mkdir(exist_ok=True)
img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(out / "icon.png")
print("build/icon.ico")
