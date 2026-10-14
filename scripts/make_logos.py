"""Make the logo files the README, the docs and PyPI use, from docs/assets/logo-original.png.

    uv run --with pillow python scripts/make_logos.py

- logo.png: the mark and the name, for light backgrounds
- logo-dark.png: the same with a light name, for dark backgrounds (GitHub's dark mode, the docs' dark scheme)
- icon.png: the mark alone, square (the docs' header)
- favicon.png: the mark, small (browser tabs)
"""

from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"
WIDTH = 1200  # sharp at the README's 360 pixels on a 3x screen
LIGHT_TEXT = (230, 237, 243)  # GitHub's dark-mode text colour
PAD = 0.03  # around the logo, as a share of its width


def is_mark(r: int, g: int, b: int) -> bool:
    return b > 120 and g > 100 and r < 120  # the cyan of the mark (anti-aliased edges included)


def main() -> None:
    original = Image.open(ASSETS / "logo-original.png").convert("RGBA")
    box = original.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    logo = original.crop(box)
    pad = int(logo.width * PAD)
    framed = Image.new("RGBA", (logo.width + 2 * pad, logo.height + 2 * pad))
    framed.paste(logo, (pad, pad))
    height = round(framed.height * WIDTH / framed.width)
    light = framed.resize((WIDTH, height), Image.LANCZOS)
    light.save(ASSETS / "logo.png", optimize=True)

    dark = light.copy()
    pixels = dark.load()
    for y in range(dark.height):
        for x in range(dark.width):
            r, g, b, a = pixels[x, y]
            if a and not is_mark(r, g, b):
                pixels[x, y] = (*LIGHT_TEXT, a)
    dark.save(ASSETS / "logo-dark.png", optimize=True)

    # The mark: the cyan part on the left.
    cyan = Image.new("L", logo.size)
    lp, cp = logo.load(), cyan.load()
    for y in range(logo.height):
        for x in range(logo.width):
            r, g, b, a = lp[x, y]
            if a > 8 and is_mark(r, g, b):
                cp[x, y] = 255
    mark = logo.crop(cyan.getbbox())
    side = round(max(mark.size) * 1.12)
    square = Image.new("RGBA", (side, side))
    square.paste(mark, ((side - mark.width) // 2, (side - mark.height) // 2))
    square.resize((512, 512), Image.LANCZOS).save(ASSETS / "icon.png", optimize=True)
    square.resize((96, 96), Image.LANCZOS).save(ASSETS / "favicon.png", optimize=True)


if __name__ == "__main__":
    main()
