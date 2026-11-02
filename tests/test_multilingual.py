"""Mixed-language command set: regression gate on the deterministic Bangla/Banglish layer (not speech recognition)."""

import unicodedata

from app.agent import multilingual_eval as M
from app.agent.language import to_english_command


def test_every_labelled_case_is_right_and_nothing_unsafe_is_rewritten():
    stats = M.evaluate()
    for lang, s in stats.items():
        assert s.unsafe_rewrites == [], (lang, s.unsafe_rewrites)
        assert s.missed == [], (lang, s.missed)
        assert s.detected == s.total, lang


def test_dataset_is_labelled_and_covers_scripts_and_tags():
    tags = {t for c in M.CASES for t in c.tags}
    assert {"script", "roman", "risky", "unsafe", "vague", "negation", "variant", "unicode", "multi"} <= tags
    assert {c.language for c in M.CASES} == {"en", "bn"} and len(M.CASES) >= 60
    assert len({c.text for c in M.CASES if c.expect}) >= 35


def test_negation_is_never_turned_into_the_command():
    for text in ("ক্রোম বন্ধ করো না", "ক্রোম খুলো না", "পিসি বন্ধ কোরো না", "chrome bondho koro na", "shutdown koro na"):
        assert to_english_command(text) is None, text
    assert to_english_command("একটু ইউটিউব খুলে দাও না") == "open youtube"        # 'na' after 'dao' is a softener


def test_the_same_word_in_either_unicode_form_is_understood():
    word = "হোয়াটসঅ্যাপ খোলো"
    for form in ("NFC", "NFD", "NFKC"):
        assert to_english_command(unicodedata.normalize(form, word)) == "open whatsapp", form


def test_app_word_suffix_vs_part_of_name():
    assert to_english_command("ক্রোম অ্যাপ খোলো") == "open chrome"
    assert to_english_command("হোয়াটস অ্যাপ খোলো") == "open whatsapp"


def test_report_mentions_limits_and_zero_unsafe():
    text = M.report()
    assert "speech recognition is not measured" in text and "(must be 0)" in text and "WRONGLY REWRITTEN" not in text
