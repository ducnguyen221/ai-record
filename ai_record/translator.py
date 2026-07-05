"""Live translation providers (SPEC.md §5.4, addendum §E4 / M2).

The default :class:`NllbTranslator` runs NLLB-200 distilled-600M via CTranslate2 int8
on **CPU** (per the ``gpu_12gb`` preset — keep the GPU free for STT). ``ctranslate2``
and ``transformers`` are imported lazily so this module is import-safe on a CPU-only
box with none of them installed. Tests inject a fake translator.

Contract highlights:
  * ``translate()`` returns the Vietnamese string, or ``None`` on error / unmapped
    language (never the source text passed off as a translation, §5.4).
  * ``is_supported(src, tgt)`` lets the pipeline distinguish a deliberate *skip*
    (unmapped language → leave ``translation`` null, no error) from a real *failure*
    (mapped language but translate returned None → ``translation_error=True``).
  * :func:`should_translate` is a pure gate (enabled ∧ lang≠target ∧ lang∈set ∧
    confident enough) so it can be unit-tested directly.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .config import Preset, Settings
from .lang_maps import nllb_code

log = logging.getLogger("ai_record.translator")


@runtime_checkable
class Translator(Protocol):
    """Pluggable translation backend (SPEC.md §5.4)."""

    name: str

    def translate(self, text: str, src_lang: str, tgt_lang: str = "vi") -> str | None: ...

    def translate_batch(self, texts: list[str], src_lang: str, tgt_lang: str = "vi") -> list[str | None]: ...

    def available(self) -> bool: ...


def should_translate(
    *,
    text: str,
    lang: str,
    lang_prob: float,
    duration: float,
    settings: Settings,
) -> bool:
    """Return True if an utterance should be translated now (SPEC.md §5.4, §E4).

    Translate only when ALL hold:
      * ``translate_enabled`` is true;
      * a detected ``lang`` exists and differs from ``target_lang``;
      * ``source_languages`` is empty (any non-target) OR ``lang`` is in it;
      * language detection is trustworthy enough — the utterance is at least
        ``translate_min_duration_s`` long AND ``lang_prob`` ≥ ``translate_min_lang_prob``.
    """
    if not settings.translate_enabled:
        return False
    if not (text or "").strip():
        return False
    target = settings.target_lang
    if not lang or lang == target:
        return False
    if settings.source_languages and lang not in settings.source_languages:
        return False
    if duration < settings.translate_min_duration_s:
        return False
    if lang_prob < settings.translate_min_lang_prob:
        return False
    return True


class NllbTranslator:
    """NLLB-200 distilled-600M via CTranslate2 int8 (default; SPEC.md §5.4)."""

    name = "nllb"

    def __init__(self, settings: Settings, preset: Preset) -> None:
        self.settings = settings
        self.preset = preset
        self.device = (settings.translation_device or preset.translation_device or "cpu")
        self._model = None
        self._tokenizer = None
        self._load_failed = False
        self._unmapped_logged: set[str] = set()

    # ------------------------------------------------------------------ #
    def is_supported(self, src_lang: str, tgt_lang: str = "vi") -> bool:
        """True if both languages have an NLLB mapping (translatable)."""
        return nllb_code(src_lang) is not None and nllb_code(tgt_lang) is not None

    def available(self) -> bool:
        """True if the CT2 model + tokenizer can be (lazily) loaded."""
        if self._load_failed:
            return False
        if self._model is not None:
            return True
        try:
            self._ensure_model()
            return True
        except Exception as exc:  # pragma: no cover - deps absent in CI
            log.warning("NLLB unavailable: %s", exc)
            self._load_failed = True
            return False

    def _ensure_model(self) -> None:
        if self._model is not None and self._tokenizer is not None:
            return
        import ctranslate2  # type: ignore
        from transformers import AutoTokenizer  # type: ignore

        model_id = self.settings.nllb_model
        # CT2-converted model dir may equal the HF id or a local path; the tokenizer
        # is always loaded from the HF id (sentencepiece).
        self._tokenizer = AutoTokenizer.from_pretrained(model_id)
        self._model = ctranslate2.Translator(model_id, device=self.device, compute_type="int8")

    # ------------------------------------------------------------------ #
    def translate(self, text: str, src_lang: str, tgt_lang: str = "vi") -> str | None:
        src = nllb_code(src_lang)
        tgt = nllb_code(tgt_lang)
        if src is None or tgt is None:
            self._log_unmapped(src_lang if src is None else tgt_lang)
            return None
        if not (text or "").strip():
            return None
        try:
            self._ensure_model()
            return self._translate_one(text, src, tgt)
        except Exception as exc:  # any failure → None + caller marks translation_error
            log.error("NLLB translate failed (%s→%s): %s", src_lang, tgt_lang, exc)
            return None

    def translate_batch(self, texts: list[str], src_lang: str, tgt_lang: str = "vi") -> list[str | None]:
        return [self.translate(t, src_lang, tgt_lang) for t in texts]

    def _translate_one(self, text: str, src: str, tgt: str) -> str | None:
        assert self._tokenizer is not None and self._model is not None
        self._tokenizer.src_lang = src
        tokens = self._tokenizer.convert_ids_to_tokens(self._tokenizer.encode(text))
        results = self._model.translate_batch(
            [tokens],
            target_prefix=[[tgt]],
            beam_size=2,
            max_decoding_length=max(64, len(tokens) * 2),
        )
        out_tokens = results[0].hypotheses[0]
        if out_tokens and out_tokens[0] == tgt:
            out_tokens = out_tokens[1:]
        return self._tokenizer.decode(
            self._tokenizer.convert_tokens_to_ids(out_tokens), skip_special_tokens=True
        ).strip() or None

    def _log_unmapped(self, lang: str) -> None:
        if lang not in self._unmapped_logged:
            self._unmapped_logged.add(lang)
            log.info("NLLB: no FLORES mapping for %r — skipping translation", lang)


class GeminiTranslator:
    """Gemini API translator (stub, off by default; SPEC.md §5.4).

    ``available()`` is true only when the ``gemini_api_key`` secret is set. Selecting
    Gemini sends text to Google — a deliberate quality/privacy tradeoff surfaced in
    Settings. Supports any language pair.
    """

    name = "gemini"

    def __init__(self, settings: Settings, secrets=None) -> None:
        self.settings = settings
        if secrets is None:
            from .config import Secrets

            secrets = Secrets()
        self.secrets = secrets
        self._client = None

    def is_supported(self, src_lang: str, tgt_lang: str = "vi") -> bool:
        return True

    def available(self) -> bool:
        return self.secrets.is_set("gemini_api_key")

    def translate(self, text: str, src_lang: str, tgt_lang: str = "vi") -> str | None:
        if not self.available():
            return None
        if not (text or "").strip():
            return None
        try:
            return self._call_gemini(text, src_lang, tgt_lang)
        except Exception as exc:  # pragma: no cover - network / SDK absent
            log.error("Gemini translate failed: %s", exc)
            return None

    def translate_batch(self, texts: list[str], src_lang: str, tgt_lang: str = "vi") -> list[str | None]:
        return [self.translate(t, src_lang, tgt_lang) for t in texts]

    def _call_gemini(self, text: str, src_lang: str, tgt_lang: str) -> str | None:  # pragma: no cover
        import google.generativeai as genai  # type: ignore

        genai.configure(api_key=self.secrets.get("gemini_api_key"))
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            f"Translate the following {src_lang} text to {tgt_lang}. "
            "Output only the translation, no notes.\n\n" + text
        )
        resp = model.generate_content(prompt)
        return (getattr(resp, "text", "") or "").strip() or None


def make_translator(settings: Settings, preset: Preset, secrets=None) -> Translator:
    """Construct the configured translator (SPEC.md §5.4)."""
    if settings.translation_provider == "gemini":
        return GeminiTranslator(settings, secrets)
    return NllbTranslator(settings, preset)
