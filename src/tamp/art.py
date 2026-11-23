"""Album art as text: each cell is "▀" with the top pixel as foreground and the bottom one as
background. Works in any truecolor terminal, inside tmux, over SSH."""

from __future__ import annotations

import io

from PIL import Image
from rich.style import Style
from rich.text import Text


def render(data: bytes, width: int, height: int) -> Text:
    """`width` x `height` cells (so width x 2*height pixels), aspect kept, centred."""
    try:
        image = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:  # corrupt or unsupported image: show nothing rather than crash
        return Text()
    image.thumbnail((width, height * 2), Image.Resampling.LANCZOS)
    w, h = image.size
    h -= h % 2
    pixels = image.load()
    pad = " " * ((width - w) // 2)
    text = Text()
    for y in range(0, h, 2):
        text.append(pad)
        for x in range(w):
            top, bottom = pixels[x, y], pixels[x, y + 1]
            text.append("▀", Style(color=f"rgb{top}", bgcolor=f"rgb{bottom}"))
        if y + 2 < h:
            text.append("\n")
    return text
