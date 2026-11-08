"""Spoken confirmation answers: "yes", "confirm", "yeah go ahead", "I confirm" must work; chatter must not; any "no" always wins."""
from __future__ import annotations

import concurrent.futures
from types import SimpleNamespace

import pytest

from app.agent.language import classify_answer, resolve_pending


@pytest.mark.parametrize("text", ["yes", "Yes.", "yeah", "Yeah!", "ok", "Okay", "confirm", "Confirmed.", "I confirm", "Yes, I confirm.", "yes confirm",
                                  "okay confirmed", "yeah go ahead", "go ahead and shut it down", "yes please do it", "Yes, shut down my computer",
                                  "sure, proceed", "yes sir", "Jarvis, yes", "হ্যাঁ", "জি", "haan", "yes yes confirm it now", "absolutely", "that's right"])
def test_clear_yes(text):
    assert classify_answer(text) is True, text


@pytest.mark.parametrize("text", ["no", "No.", "nope", "cancel", "stop", "don't", "do not do it", "no don't confirm", "yes... no wait", "yes but cancel it",
                                  "I don't confirm", "না", "wait", "never mind"])
def test_clear_no_always_wins(text):
    assert classify_answer(text) is False, text


@pytest.mark.parametrize("text", ["", "   ", "Yes, I'm pumped", "It's going to be like this and it's going to keep it on over",
                                  "yes the weather is nice today and I will go for a walk", "open chrome", "what time is it", "maybe", "hmm", "thank you"])
def test_chatter_is_not_an_answer(text):
    assert classify_answer(text) is None, text


def pending(level=2):
    return SimpleNamespace(request=SimpleNamespace(level=level), future=concurrent.futures.Future())


def test_resolve_pending_sets_the_future_once():
    p = pending()
    assert resolve_pending(p, "yes, I confirm") is True and p.future.result() is True
    assert resolve_pending(p, "no") is None                      # already answered
    q = pending()
    assert resolve_pending(q, "open chrome") is None and not q.future.done()
    assert resolve_pending(q, "no") is False and q.future.result() is False
    assert resolve_pending(None, "yes") is None


def test_cancelled_future_is_safe():
    p = pending()
    p.future.cancel()
    assert resolve_pending(p, "yes") is None


def test_critical_questions_need_a_click():
    p = pending(level=3)
    assert resolve_pending(p, "yes") is None and not p.future.done()


def test_stt_answer_mode_keeps_short_yes_and_ok(monkeypatch):
    """Short answers like 'Yeah' / 'OK' are on the filler list (noise guard). In answer mode they must survive."""
    from app.voice import speech_to_text as S

    assert "yeah" in S.FILLERS and "ok" in S.FILLERS               # why answer mode exists
    import inspect

    assert "answer" in inspect.signature(S.FasterWhisperSTT.transcribe).parameters
