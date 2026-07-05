"""Whisper (ISO-639-1) ↔ NLLB (FLORES-200) language code maps (SPEC.md §5.4).

Import-safe with no heavy dependencies. Used by :mod:`ai_record.translator` for the
NLLB code mapping and by the ``/api/languages`` endpoint for the source-language
picker. Unmapped languages are skipped by the translator (logged once, §5.4).
"""

from __future__ import annotations

# Whisper ISO-639-1 → NLLB FLORES-200 code. Ships at least the SPEC.md §5.4 set,
# plus a few common extras. Anything not here is treated as "unmapped" → skipped.
WHISPER_TO_NLLB: dict[str, str] = {
    "ja": "jpn_Jpan",
    "vi": "vie_Latn",
    "en": "eng_Latn",
    "zh": "zho_Hans",
    "ko": "kor_Hang",
    "fr": "fra_Latn",
    "de": "deu_Latn",
    "es": "spa_Latn",
    "ru": "rus_Cyrl",
    "th": "tha_Thai",
    # Common extras (safe to translate to/from):
    "pt": "por_Latn",
    "it": "ita_Latn",
    "id": "ind_Latn",
    "nl": "nld_Latn",
    "hi": "hin_Deva",
    "ar": "arb_Arab",
    "tr": "tur_Latn",
    "pl": "pol_Latn",
}

# Human-readable names for the source-language picker (/api/languages).
LANGUAGE_NAMES: dict[str, str] = {
    "ja": "Japanese",
    "vi": "Vietnamese",
    "en": "English",
    "zh": "Chinese",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "ru": "Russian",
    "th": "Thai",
    "pt": "Portuguese",
    "it": "Italian",
    "id": "Indonesian",
    "nl": "Dutch",
    "hi": "Hindi",
    "ar": "Arabic",
    "tr": "Turkish",
    "pl": "Polish",
}


def nllb_code(iso: str) -> str | None:
    """Return the NLLB FLORES code for a Whisper ISO-639-1 code, or ``None`` if unmapped."""
    if not iso:
        return None
    return WHISPER_TO_NLLB.get(iso.lower())


def is_mapped(iso: str) -> bool:
    """True if ``iso`` has an NLLB mapping (i.e. NLLB can translate it)."""
    return nllb_code(iso) is not None


def supported_source_languages() -> list[dict[str, str]]:
    """Return the supported source languages for the translation UI (``/api/languages``)."""
    out: list[dict[str, str]] = []
    for code in WHISPER_TO_NLLB:
        out.append({"code": code, "name": LANGUAGE_NAMES.get(code, code)})
    out.sort(key=lambda d: d["name"])
    return out
