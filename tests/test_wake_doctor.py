from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.voice import wake_doctor, wake_word


@pytest.fixture(autouse=True)
def reset_aliases():
    wake_word.set_personal_aliases([])
    yield
    wake_word.set_personal_aliases([])


def test_before_learning_a_misspelt_name_does_not_wake() -> None:
    assert wake_word.strip_wake_phrase("Hey Travis open chrome", "hey jarvis") == ("Hey Travis open chrome", False)


def test_an_approved_alias_wakes_but_only_in_the_name_position() -> None:
    wake_word.set_personal_aliases(["travis"])
    assert wake_word.strip_wake_phrase("Hey Travis open chrome", "hey jarvis") == ("open chrome", True)
    assert wake_word.strip_wake_phrase("Travis, what time is it", "hey jarvis") == ("what time is it", True)
    assert wake_word.strip_wake_phrase("open chrome for Travis", "hey jarvis")[1] is False    # not at the start
    assert wake_word.strip_wake_phrase("Hey Davis open chrome", "hey jarvis")[1] is False     # other names are unaffected


def test_an_alias_never_replaces_the_greeting() -> None:
    wake_word.set_personal_aliases(["travis"])
    assert wake_word.strip_wake_phrase("Travis Travis open chrome", "hey jarvis")[1] is True   # name alone is the existing rule
    assert wake_word.strip_wake_phrase("Banana Travis open chrome", "hey jarvis")[1] is False


def test_aliases_are_stored_validated_and_capped(tmp_path: Path) -> None:
    wake_doctor.save(tmp_path, ["Travis", "travis", "gervais"])
    assert wake_doctor.load(tmp_path) == ["gervais", "travis"]
    (tmp_path / wake_doctor.FILE).write_text(json.dumps({"aliases": ["ok", "bad word!", "<script>", 5]}), encoding="utf-8")
    assert wake_doctor.load(tmp_path) == ["ok"]
    (tmp_path / wake_doctor.FILE).write_text("not json", encoding="utf-8")
    assert wake_doctor.load(tmp_path) == []
    wake_doctor.save(tmp_path, [f"name{chr(97 + i)}" for i in range(20)])
    assert len(wake_doctor.load(tmp_path)) == wake_doctor.MAX_ALIASES


def test_the_name_token_is_found_after_a_greeting() -> None:
    assert wake_doctor.name_token("Hey Travis open chrome", "hey jarvis") == "travis"
    assert wake_doctor.name_token("Travis open chrome", "hey jarvis") == "travis"
    assert wake_doctor.name_token("", "hey jarvis") == ""


def test_analysis_only_suggests_consistent_similar_sounding_names() -> None:
    report = wake_doctor.analyse(["Hey Travis open chrome", "Hey Travis", "Hey Jarvis", "Hey banana", "weather today"], "hey jarvis")
    assert [a.accepted for a in report.attempts] == [False, False, True, False, False]
    assert report.suggestions == ["travis"]                      # 'banana' sounds nothing like it; 'weather' neither
    assert report.hit_rate == pytest.approx(0.2)


def run(tmp_path: Path, heard: list[str], answers: list[str]):
    spoken = iter(heard)
    out = io.StringIO()
    code = wake_doctor.run(SimpleNamespace(wake_word="hey jarvis"), tmp_path, out=out, ask=lambda q: answers.pop(0),
                           listen=lambda: next(spoken), rounds=len(heard))
    return code, out.getvalue()


def test_the_flow_learns_an_alias_only_after_a_yes(tmp_path: Path) -> None:
    code, text = run(tmp_path, ["Hey Travis", "Hey Travis open chrome", "Hey Jarvis", "Hey Travis", "Hey Jarvis", "hey travis"], ["y"])
    assert "Saved" in text and wake_doctor.load(tmp_path) == ["travis"] and code == 0
    assert wake_word.strip_wake_phrase("Hey Travis", "hey jarvis")[1] is True       # active immediately


def test_saying_no_saves_nothing_and_explains(tmp_path: Path) -> None:
    code, text = run(tmp_path, ["Hey Travis", "Hey Travis", "Hey Travis", "Hey Jarvis"], ["n"])
    assert wake_doctor.load(tmp_path) == [] and code == 1 and "Saved" not in text
    assert wake_word.strip_wake_phrase("Hey Travis", "hey jarvis")[1] is False


def test_silence_points_to_the_microphone_not_the_wake_word(tmp_path: Path) -> None:
    code, text = run(tmp_path, ["", "", "", "Hey Jarvis"], [])
    assert code == 1 and "--voice-doctor" in text and "the problem is the microphone" in text


def test_a_working_wake_phrase_asks_nothing(tmp_path: Path) -> None:
    code, text = run(tmp_path, ["Hey Jarvis", "Hey Jarvis open chrome", "Hey Jarvis"], [])
    assert code == 0 and "Would have woken 3 of 3" in text
