#!/usr/bin/env python3
"""Render assets/sshtree.ico from the app's built-in pixel icon (needs Pillow).

The window icon inside the app is drawn from a tiny pixel map so that the
program stays dependency-free; the executable icon is rendered from the same
map at several sizes here, at build time.  The .ico container is written by
hand so every size is a crisp nearest-neighbour upscale (Pillow's ICO writer
would resample the largest frame down instead).
"""

import io
import struct
from pathlib import Path

from PIL import Image

ROWS = [
    "................",
    ".bbbbbbbbbbbbbb.",
    ".bwwwwwwwwwwwwb.",
    ".bwkkkkkkkkkkwb.",
    ".bwkgkkkkkkkkwb.",
    ".bwkkgkkkkkkkwb.",
    ".bwkgkkkkkkkkwb.",
    ".bwkkkkkkkkkkwb.",
    ".bwkkkkkkkkkkwb.",
    ".bwwwwwwwwwwwwb.",
    ".bbbbbbbbbbbbbb.",
    "......bbbb......",
    "......bbbb......",
    "....bbbbbbbb....",
    "................",
    "................",
]
PALETTE = {
    "b": (59, 95, 138, 255),
    "w": (200, 216, 234, 255),
    "k": (16, 24, 32, 255),
    "g": (64, 224, 96, 255),
    ".": (0, 0, 0, 0),
}
SIZES = [16, 24, 32, 48, 64, 128, 256]


def base_image() -> Image.Image:
    img = Image.new("RGBA", (16, 16))
    for y, row in enumerate(ROWS):
        for x, ch in enumerate(row):
            img.putpixel((x, y), PALETTE[ch])
    return img


def write_ico(frames: list[Image.Image], out: Path) -> None:
    blobs = []
    for frame in frames:
        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        blobs.append(buf.getvalue())
    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = len(header) + 16 * len(frames)
    entries = b""
    for frame, blob in zip(frames, blobs):
        w, h = frame.size
        entries += struct.pack(
            "<BBBBHHII", w % 256, h % 256, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)
    out.write_bytes(header + entries + b"".join(blobs))


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "assets" / "sshtree.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    base = base_image()
    frames = [base.resize((s, s), Image.NEAREST) for s in SIZES]
    write_ico(frames, out)
    print(f"wrote {out} ({out.stat().st_size} bytes, sizes {SIZES})")


if __name__ == "__main__":
    main()
