"""Colour themes: built-in palettes, your own (TOML files), and "auto", which takes its colours
from the playing album's art and falls back to your terminal's own colours when nothing plays.

A palette names roles, not hues: `accent` is the brand colour (artists, borders), `playing` the
progress bar and ▶ marker, `highlight` the sung lyric line and key hints, and so on."""

from __future__ import annotations

import colorsys
import io
import re
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

ANSI = {
    "default", "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "bright_black", "bright_red", "bright_green", "bright_yellow", "bright_blue",
    "bright_magenta", "bright_cyan", "bright_white",
}  # fmt: skip
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str  # the main background
    panel: str  # top bar, footer, pop-ups
    overlay: str  # behind pop-ups (dimmed)
    line: str  # borders and separators
    select: str  # the highlighted row
    hover: str  # a row under the mouse
    fg: str  # text
    dim: str  # secondary text
    accent: str  # artists, titles, menu borders
    playing: str  # ▶, the progress bar, "playing"
    highlight: str  # the sung lyric line, key hints, search box
    album: str  # album names, the device you're playing on
    extra: str  # liked hearts, track numbers in places
    warm: str  # radio, stations

    @property
    def ansi(self) -> bool:
        return self.bg in ANSI


def css(colour: str) -> str:
    """A palette colour as Textual CSS (ANSI names need the ansi_ prefix there)."""
    return f"ansi_{colour}" if colour in ANSI else colour


def _p(name: str, *colours: str) -> Palette:
    return Palette(name, *colours)


#                  bg         panel      overlay    line       select     hover      fg         dim
#                  accent     playing    highlight  album      extra      warm
BUILT_IN = {
    p.name: p
    for p in (
        _p("monokai-pro", "#2d2a2e", "#221f22", "#19181a", "#403e41", "#5b595c", "#363337", "#fcfcfa", "#727072",
           "#ff6188", "#a9dc76", "#ffd866", "#78dce8", "#ab9df2", "#fc9867"),
        _p("catppuccin-mocha", "#1e1e2e", "#181825", "#11111b", "#313244", "#45475a", "#2a2b3c", "#cdd6f4", "#6c7086",
           "#f38ba8", "#a6e3a1", "#f9e2af", "#89dceb", "#cba6f7", "#fab387"),
        _p("catppuccin-latte", "#eff1f5", "#e6e9ef", "#dce0e8", "#ccd0da", "#bcc0cc", "#e6e9ef", "#4c4f69", "#8c8fa1",
           "#d20f39", "#40a02b", "#df8e1d", "#04a5e5", "#8839ef", "#fe640b"),
        _p("dracula", "#282a36", "#21222c", "#191a21", "#343746", "#44475a", "#303241", "#f8f8f2", "#6272a4",
           "#ff79c6", "#50fa7b", "#f1fa8c", "#8be9fd", "#bd93f9", "#ffb86c"),
        _p("solarized-dark", "#002b36", "#00212b", "#00161c", "#073642", "#0b4352", "#03313d", "#93a1a1", "#586e75",
           "#d33682", "#859900", "#b58900", "#2aa198", "#6c71c4", "#cb4b16"),
        _p("solarized-light", "#fdf6e3", "#eee8d5", "#e4ddc8", "#e4ddc8", "#d9d2bd", "#f5efdc", "#586e75", "#93a1a1",
           "#d33682", "#859900", "#b58900", "#2aa198", "#6c71c4", "#cb4b16"),
        _p("gruvbox-dark", "#282828", "#1d2021", "#141617", "#3c3836", "#504945", "#32302f", "#ebdbb2", "#928374",
           "#fb4934", "#b8bb26", "#fabd2f", "#8ec07c", "#d3869b", "#fe8019"),
        _p("nord", "#2e3440", "#272c36", "#20242c", "#3b4252", "#4c566a", "#353b49", "#eceff4", "#616e88",
           "#bf616a", "#a3be8c", "#ebcb8b", "#88c0d0", "#b48ead", "#d08770"),
        _p("tokyo-night", "#1a1b26", "#16161e", "#101014", "#292e42", "#3b4261", "#222433", "#c0caf5", "#565f89",
           "#f7768e", "#9ece6a", "#e0af68", "#7dcfff", "#bb9af7", "#ff9e64"),
        _p("rose-pine", "#191724", "#1f1d2e", "#12101a", "#26233a", "#403d52", "#21202e", "#e0def4", "#6e6a86",
           "#eb6f92", "#9ccfd8", "#f6c177", "#ebbcba", "#c4a7e7", "#ebbcba"),
        # your terminal's own colours (and background, so a transparent terminal stays transparent)
        _p("terminal", "default", "default", "default", "bright_black", "bright_black", "default", "default",
           "bright_black", "magenta", "green", "yellow", "cyan", "blue", "red"),
    )
}  # fmt: skip
DEFAULT = "monokai-pro"
AUTO = "auto"  # from the playing album's art; your terminal's colours when nothing plays

_active = BUILT_IN[DEFAULT]


def current() -> Palette:
    return _active


def activate(palette: Palette) -> None:
    global _active
    _active = palette


def css_variables(palette: Palette) -> dict[str, str]:
    """$t-bg, $t-accent, … for tamp.tcss."""
    return {f"t-{f.name}": css(getattr(palette, f.name)) for f in fields(Palette) if f.name != "name"}


# ── your own themes ──
def load_user_themes(directory: Path) -> tuple[dict[str, Palette], list[str]]:
    """Every *.toml in `directory`: `extends = "dracula"` plus any colours to change.
    Returns the themes and a message for each file that couldn't be used."""
    themes: dict[str, Palette] = {}
    problems: list[str] = []
    for path in sorted(directory.glob("*.toml")) if directory.is_dir() else []:
        try:
            theme = parse_user_theme(path.stem, tomllib.loads(path.read_text()))
        except (OSError, tomllib.TOMLDecodeError, ValueError) as e:
            problems.append(f"{path.name}: {e}")
            continue
        themes[theme.name] = theme
    return themes, problems


def parse_user_theme(stem: str, data: dict) -> Palette:
    base_name = data.get("extends", DEFAULT)
    if base_name not in BUILT_IN:
        raise ValueError(f"extends {base_name!r}: not a built-in theme ({', '.join(BUILT_IN)})")
    colours = {}
    roles = {f.name for f in fields(Palette)} - {"name"}
    for key, value in data.items():
        if key in ("extends", "name"):
            continue
        if key not in roles:
            raise ValueError(f"unknown colour {key!r} (use: {', '.join(sorted(roles))})")
        if not isinstance(value, str) or not (HEX.match(value) or value in ANSI):
            raise ValueError(f"{key} = {value!r}: use #rrggbb or a terminal colour name like bright_blue")
        colours[key] = value
    name = str(data.get("name", stem))
    return replace(BUILT_IN[base_name], name=name, **colours)


# ── auto: colours from album art ──
def _hex(r: float, g: float, b: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(round(max(0.0, min(1.0, c)) * 255) for c in (r, g, b)))


def _hls(hue: float, lightness: float, saturation: float) -> str:
    return _hex(*colorsys.hls_to_rgb(hue % 1.0, lightness, saturation))


def from_art(image: bytes, name: str = AUTO) -> Palette | None:
    """A dark palette tinted by the art's main colour, with its most vivid colours as accents
    (lifted so they read on the dark background). None if the image can't be read."""
    try:
        from PIL import Image

        picture = Image.open(io.BytesIO(image)).convert("RGB").resize((48, 48))
        quantised = picture.quantize(colors=8)
    except Exception:
        return None
    flat = quantised.getpalette() or []
    counts = sorted(quantised.getcolors() or [], reverse=True)  # (count, index), most common first
    if not counts:
        return None
    swatches = [
        (count, colorsys.rgb_to_hls(*(flat[i * 3 + c] / 255 for c in range(3)))) for count, i in counts
    ]
    _, (base_hue, _, base_sat) = swatches[0]

    def vividness(swatch: tuple[int, tuple[float, float, float]]) -> float:
        count, (_, light, sat) = swatch
        return sat * (1 - abs(light - 0.5) * 1.6) * count**0.35

    vivid = sorted(swatches, key=vividness, reverse=True)
    first_hue, _, first_sat = vivid[0][1]
    second = next(
        (hls for _, hls in vivid[1:] if min(abs(hls[0] - first_hue), 1 - abs(hls[0] - first_hue)) > 0.08),
        (first_hue + 0.45, 0.5, first_sat),
    )
    tint = min(base_sat, 0.45)
    accent_sat = max(first_sat, 0.55)
    return Palette(
        name=name,
        bg=_hls(base_hue, 0.11, tint),
        panel=_hls(base_hue, 0.08, tint),
        overlay=_hls(base_hue, 0.05, tint),
        line=_hls(base_hue, 0.2, tint),
        select=_hls(base_hue, 0.27, tint),
        hover=_hls(base_hue, 0.16, tint),
        fg=_hls(base_hue, 0.94, 0.15),
        dim=_hls(base_hue, 0.58, 0.12),
        accent=_hls(first_hue, 0.68, accent_sat),
        playing=_hls(first_hue, 0.62, accent_sat),
        highlight=_hls(first_hue, 0.82, min(1.0, accent_sat + 0.1)),
        album=_hls(second[0], 0.72, max(second[2], 0.45)),
        extra=_hls(second[0], 0.66, max(second[2], 0.4)),
        warm=_hls(first_hue + 0.06, 0.7, accent_sat),
    )
