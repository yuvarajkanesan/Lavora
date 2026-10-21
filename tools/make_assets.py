"""Generates assets/lavora.ico and the installer wizard artwork with Pillow.

PLACEHOLDER ART (TODO: replace with the real Lavora/Lavati branding before a public release).
Run from the project folder:  python tools/make_assets.py
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from lavora import __version__  # noqa: E402
from lavora.brand import NAME, PUBLISHER, TAGLINE  # noqa: E402

PURPLE, PURPLE_DARK, LIGHT, TEXT, SUB = (91, 63, 209), (52, 33, 140), (246, 245, 251), (28, 27, 34), (106, 103, 120)


def font(size: int, bold: bool = False):
    for name in ("segoeuib.ttf" if bold else "segoeui.ttf", "arialbd.ttf" if bold else "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def mark(size: int) -> Image.Image:
    """A rounded square with a white L and a tick: 'it works again'."""
    s = size * 4  # draw large, downsample for smooth edges
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for y in range(s):  # vertical gradient
        t = y / s
        col = tuple(int(PURPLE[i] * (1 - t) + PURPLE_DARK[i] * t) for i in range(3)) + (255,)
        d.line([(0, y), (s, y)], fill=col)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
    im.putalpha(mask)
    d = ImageDraw.Draw(im)
    w = int(s * 0.12)
    d.line([(s * 0.30, s * 0.22), (s * 0.30, s * 0.74), (s * 0.58, s * 0.74)], fill="white", width=w, joint="curve")
    d.line([(s * 0.50, s * 0.46), (s * 0.62, s * 0.58), (s * 0.84, s * 0.28)], fill=(160, 240, 190), width=int(w * 0.8), joint="curve")
    return im.resize((size, size), Image.LANCZOS)


def wizard_large(path: str) -> None:
    w, h = 328, 628
    im = Image.new("RGB", (w, h), LIGHT)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w, 8], fill=PURPLE)
    for y in range(int(h * 0.72), h):  # soft fade at the bottom
        t = (y - h * 0.72) / (h * 0.28)
        d.line([(0, y), (w, y)], fill=tuple(int(LIGHT[i] * (1 - t) + (201, 189, 230)[i] * t) for i in range(3)))
    im.paste(mark(150), ((w - 150) // 2, 70), mark(150))
    for text, y, f, col in ((NAME, 250, font(40, True), TEXT), ("get your system working again", 305, font(15), SUB),
                            (f"Version {__version__}", 570, font(13), SUB), (PUBLISHER, 592, font(13), SUB)):
        tw = d.textlength(text, font=f)
        d.text(((w - tw) / 2, y), text, font=f, fill=col)
    im.save(path, "BMP")


def wizard_small(path: str) -> None:
    im = Image.new("RGB", (110, 110), LIGHT)
    m = mark(80)
    im.paste(m, (15, 15), m)
    im.save(path, "BMP")


def main() -> None:
    os.makedirs(os.path.join(ROOT, "assets"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "installer"), exist_ok=True)
    big = mark(256)
    big.save(os.path.join(ROOT, "assets", "lavora.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(os.path.join(ROOT, "assets", "lavora.png"))
    wizard_large(os.path.join(ROOT, "installer", "wizard-large.bmp"))
    wizard_small(os.path.join(ROOT, "installer", "wizard-small.bmp"))
    print("assets written (placeholder art)", TAGLINE)


if __name__ == "__main__":
    main()
