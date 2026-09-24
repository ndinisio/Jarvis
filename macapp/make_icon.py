"""Draw JARVIS's app icon (Resources/AppIcon.icns): the interface's glowing
ring on the macOS rounded square. Needs Pillow; run with any Python 3.

The .icns format is a list of PNGs, so no Apple tools are needed.
"""

from __future__ import annotations

import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
SIZES = {"ic07": 128, "ic08": 256, "ic09": 512, "ic10": 1024}


def draw(size: int) -> Image.Image:
    scale = 4                                           # draw big, shrink smooth
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    inset = int(big * 0.098)                            # the macOS icon grid's margin
    radius = int((big - 2 * inset) * 0.225)
    square = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(square).rounded_rectangle((inset, inset, big - inset, big - inset), radius,
                                             fill=(7, 11, 17, 255))
    shading = Image.new("L", (big, big), 0)
    ImageDraw.Draw(shading).ellipse((big * 0.1, -big * 0.35, big * 0.9, big * 0.55), fill=40)
    shading = shading.filter(ImageFilter.GaussianBlur(big * 0.08))
    square.paste((40, 90, 120, 255), mask=Image.composite(shading, Image.new("L", (big, big), 0),
                                                          square.split()[3]))
    image.alpha_composite(square)

    centre, ring = big / 2, big * 0.25
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((centre - ring, centre - ring, centre + ring, centre + ring),
                                 outline=(95, 211, 243, 255), width=int(big * 0.035))
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(big * 0.03)))
    image.alpha_composite(glow)
    core = big * 0.07
    dot = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(dot).ellipse((centre - core, centre - core, centre + core, centre + core),
                                fill=(207, 243, 255, 255))
    image.alpha_composite(dot.filter(ImageFilter.GaussianBlur(big * 0.02)))
    image.alpha_composite(dot)
    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    entries = b""
    for kind, size in SIZES.items():
        buffer = io.BytesIO()
        draw(size).save(buffer, format="PNG")
        data = buffer.getvalue()
        entries += kind.encode() + struct.pack(">I", len(data) + 8) + data
    target = HERE / "Resources" / "AppIcon.icns"
    target.write_bytes(b"icns" + struct.pack(">I", len(entries) + 8) + entries)
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
