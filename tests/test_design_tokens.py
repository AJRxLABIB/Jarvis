from __future__ import annotations

import json
import re

from app.core import a11y
from app.ui.styles import theme
from app.ui.styles.tokens import COLOR, RADIUS, SPACING, STATE_COLOR, as_dict

HEX = re.compile(r"#[0-9a-fA-F]{6}")


def test_tokens_are_valid_colours_and_json_exportable() -> None:
    for value in [*COLOR.values(), *STATE_COLOR.values()]:
        assert HEX.fullmatch(value)
    assert all(isinstance(v, int) and v >= 0 for v in [*SPACING.values(), *RADIUS.values()])
    assert json.loads(json.dumps(as_dict()))["color"]["accent"] == COLOR["accent"]


def test_the_theme_is_built_from_the_tokens() -> None:
    assert theme.BG == COLOR["bg"] and theme.ACCENT == COLOR["accent"] and theme.STATE_COLORS == STATE_COLOR
    assert "{C[" not in theme.STYLESHEET and "{SANS}" not in theme.STYLESHEET
    assert COLOR["accent_strong"] in theme.STYLESHEET and COLOR["danger_bg"] in theme.STYLESHEET


def test_every_stylesheet_colour_is_a_token_or_a_known_neutral() -> None:
    allowed = {v.lower() for v in [*COLOR.values(), *STATE_COLOR.values()]} | {"#ffffff", "#fff4dc"}
    used = {m.lower() for m in HEX.findall(theme.STYLESHEET)}
    assert used <= allowed, used - allowed


def test_token_contrast_still_passes_the_audit() -> None:
    assert a11y.audit_theme() == []


def test_widgets_use_tokens_instead_of_repeating_their_colour_values() -> None:
    """A colour that already has a token must be written as the token (a quoted bare literal is the drift we removed)."""
    from pathlib import Path

    known = {v.lower(): k for k, v in COLOR.items()}
    offenders = []
    for path in (Path(__file__).resolve().parents[1] / "app" / "ui").rglob("*.py"):
        if path.parent.name == "styles":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for quote in ('"', "'"):
                for value, name in known.items():
                    if f"{quote}{value}{quote}" in line.lower():
                        offenders.append(f"{path.name}:{number} {value} -> COLOR['{name}']")
    assert not offenders, offenders
