"""M2 translator: when-to-translate gate, NLLB mapping/error handling, Gemini gating."""

from __future__ import annotations

import pytest

from ai_record.config import Secrets, Settings, resolve_preset
from ai_record.translator import (
    GeminiTranslator,
    NllbTranslator,
    make_translator,
    should_translate,
)


def _settings(**kw) -> Settings:
    base = dict(
        hardware_preset="cpu",
        translate_enabled=True,
        target_lang="vi",
        translate_min_duration_s=1.0,
        translate_min_lang_prob=0.6,
    )
    base.update(kw)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# should_translate (pure gate)
# --------------------------------------------------------------------------- #
def test_should_translate_happy_path():
    s = _settings(source_languages=["ja"])
    assert should_translate(text="こんにちは", lang="ja", lang_prob=0.9, duration=2.0, settings=s)


def test_should_translate_skips_target_language():
    s = _settings(source_languages=[])
    assert not should_translate(text="xin chào", lang="vi", lang_prob=0.9, duration=2.0, settings=s)


def test_should_translate_respects_source_language_filter():
    s = _settings(source_languages=["ja"])
    assert not should_translate(text="hello", lang="en", lang_prob=0.9, duration=2.0, settings=s)


def test_should_translate_empty_filter_means_any_nontarget():
    s = _settings(source_languages=[])
    assert should_translate(text="hello", lang="en", lang_prob=0.9, duration=2.0, settings=s)


def test_should_translate_defers_short_or_low_confidence():
    s = _settings(source_languages=[])
    assert not should_translate(text="a", lang="ja", lang_prob=0.9, duration=0.5, settings=s)
    assert not should_translate(text="a", lang="ja", lang_prob=0.3, duration=2.0, settings=s)


def test_should_translate_disabled():
    s = _settings(translate_enabled=False, source_languages=[])
    assert not should_translate(text="hi", lang="ja", lang_prob=0.9, duration=2.0, settings=s)


# --------------------------------------------------------------------------- #
# NllbTranslator
# --------------------------------------------------------------------------- #
def test_nllb_is_supported_and_unmapped_skip():
    s = _settings()
    t = NllbTranslator(s, resolve_preset(s))
    assert t.is_supported("ja", "vi")
    assert not t.is_supported("xx", "vi")
    # Unmapped source → None WITHOUT ever loading a model (skip, not error).
    assert t.translate("blah", "xx", "vi") is None


def test_nllb_error_returns_none(monkeypatch):
    s = _settings()
    t = NllbTranslator(s, resolve_preset(s))

    def boom():
        raise RuntimeError("ctranslate2 missing")

    monkeypatch.setattr(t, "_ensure_model", boom)
    assert t.translate("こんにちは", "ja", "vi") is None


def test_nllb_success_via_mock(monkeypatch):
    s = _settings()
    t = NllbTranslator(s, resolve_preset(s))
    monkeypatch.setattr(t, "_ensure_model", lambda: None)
    monkeypatch.setattr(t, "_translate_one", lambda text, src, tgt: "xin chào")
    assert t.translate("こんにちは", "ja", "vi") == "xin chào"


def test_make_translator_selects_provider():
    s = _settings(translation_provider="nllb")
    assert isinstance(make_translator(s, resolve_preset(s)), NllbTranslator)
    s2 = _settings(translation_provider="gemini")
    assert isinstance(make_translator(s2, resolve_preset(s2)), GeminiTranslator)


# --------------------------------------------------------------------------- #
# GeminiTranslator gating
# --------------------------------------------------------------------------- #
def test_gemini_available_requires_key():
    s = _settings(translation_provider="gemini")
    sec = Secrets()
    sec.clear("gemini_api_key")
    g = GeminiTranslator(s, sec)
    assert not g.available()
    assert g.translate("hi", "en", "vi") is None
    sec.set("gemini_api_key", "k")
    try:
        assert g.available()
    finally:
        sec.clear("gemini_api_key")
