"""Themes: built-ins, your own TOML themes, and auto colours from album art."""

import io

import pytest
from PIL import Image

from tamp.ui import fmt, theme


def png(colour: tuple[int, int, int], accent: tuple[int, int, int]) -> bytes:
    picture = Image.new("RGB", (60, 60), colour)
    for x in range(20, 40):
        for y in range(20, 40):
            picture.putpixel((x, y), accent)
    out = io.BytesIO()
    picture.save(out, "PNG")
    return out.getvalue()


def lightness(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return (max(r, g, b) + min(r, g, b)) / 2


def test_every_built_in_has_valid_colours() -> None:
    for palette in theme.BUILT_IN.values():
        for name, value in theme.css_variables(palette).items():
            assert theme.HEX.match(value) or value.startswith("ansi_"), (palette.name, name, value)


def test_fmt_colours_follow_the_active_theme() -> None:
    try:
        theme.activate(theme.BUILT_IN["dracula"])
        assert fmt.PINK == "#ff79c6"
        theme.activate(theme.BUILT_IN["terminal"])
        assert fmt.PINK == "magenta" and theme.css("magenta") == "ansi_magenta"
    finally:
        theme.activate(theme.BUILT_IN[theme.DEFAULT])


def test_a_user_theme_extends_a_built_in(tmp_path) -> None:
    (tmp_path / "mine.toml").write_text('extends = "nord"\naccent = "#ff0000"\n')
    (tmp_path / "broken.toml").write_text('accent = "red-ish"\n')
    themes, problems = theme.load_user_themes(tmp_path)
    assert themes["mine"].accent == "#ff0000" and themes["mine"].bg == theme.BUILT_IN["nord"].bg
    assert len(problems) == 1 and "broken.toml" in problems[0]


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown colour"):
        theme.parse_user_theme("x", {"accnt": "#ffffff"})


def test_auto_palette_is_dark_tinted_and_its_accents_readable() -> None:
    blue_album = png((20, 120, 210), (230, 200, 40))  # mostly blue, a yellow square
    palette = theme.from_art(blue_album)
    assert palette is not None
    assert lightness(palette.bg) < 0.2  # dark background
    r, _, b = (int(palette.bg[i : i + 2], 16) for i in (1, 3, 5))
    assert b > r  # tinted by the art's main colour
    for role in ("accent", "playing", "highlight", "album", "fg"):
        assert lightness(getattr(palette, role)) > 0.5, role  # reads on the dark background


def test_auto_palette_ignores_garbage() -> None:
    assert theme.from_art(b"not an image") is None


async def test_previewing_a_theme_in_auto_is_not_repainted() -> None:
    from tamp.app import TampApp

    app = TampApp(demo=True)
    app.cfg = app.cfg.__class__(**{**app.cfg.__dict__, "theme": "auto"})
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("T")
        await pilot.pause(0.3)
        await pilot.press("down", "down", "down")  # auto → monokai → mocha → latte
        await pilot.pause(0.2)
        app.on_player_update(app.player.state, app.player.queue)  # a tick from the player
        await pilot.pause(0.2)
        assert theme.current().name == "catppuccin-latte"
        await pilot.press("escape")
        await pilot.pause(0.2)
    theme.activate(theme.BUILT_IN[theme.DEFAULT])


def test_action_rows_take_the_colour_of_the_theme_in_use() -> None:
    try:
        for name in ("dracula", "nord"):
            theme.activate(theme.BUILT_IN[name])
            row = fmt.action_row("+", "New playlist…")
            assert str(row.spans[0].style) == theme.BUILT_IN[name].playing, name
    finally:
        theme.activate(theme.BUILT_IN[theme.DEFAULT])
