"""Agent-readable exports of transcript / summary / combined (addendum §E3).

Two formats:
  * ``.md`` — YAML front-matter (session_id, title, date, duration, sources, languages,
    model) + a clean, parseable body. One block per utterance
    ``**[hh:mm:ss] Speaker (lang):** text`` with the translation as a ``> `` quote line.
  * ``.txt`` — the same information with no Markdown markup; translation on the next
    indented line.

Pure string builders (no I/O) so they are trivially unit-testable; the server wraps
the output in a ``Content-Disposition: attachment`` download.
"""

from __future__ import annotations

from typing import Any

WHAT_VALUES = ("transcript", "summary", "combined")
FMT_VALUES = ("md", "txt")


def _fmt_hhmmss(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _languages(records) -> list[str]:
    seen: list[str] = []
    for rec in records:
        if rec.lang and rec.lang not in seen:
            seen.append(rec.lang)
    return seen


def _meta_dict(meta: Any) -> dict:
    return meta.to_dict() if hasattr(meta, "to_dict") else dict(meta)


def _yaml_front_matter(meta: Any, records) -> str:
    m = _meta_dict(meta)
    sources = [k for k, v in (m.get("sources") or {}).items() if v]
    langs = _languages(records)
    lines = [
        "---",
        f"session_id: {m.get('session_id', '')}",
        f"title: {_yaml_str(m.get('title', ''))}",
        f"date: {(m.get('created_at') or '')[:10]}",
        f"duration_sec: {m.get('duration_sec') if m.get('duration_sec') is not None else ''}",
        f"sources: [{', '.join(sources)}]",
        f"languages: [{', '.join(langs)}]",
        f"model: {m.get('whisper_model', '')}",
        f"mode: {m.get('mode', 'meeting')}",
        "---",
    ]
    return "\n".join(lines)


def _yaml_str(value: str) -> str:
    value = value or ""
    # Newlines would break the single-line front-matter scalar → fold to spaces.
    value = value.replace("\r", " ").replace("\n", " ")
    if any(c in value for c in ':#[]{}\"') or value != value.strip():
        return '"' + value.replace('"', '\\"') + '"'
    return value


def _safe_speaker(name: str) -> str:
    """Neutralize markdown-/layout-breaking speaker labels in the export body: fold
    newlines and drop emphasis/heading control chars so a hostile label can't break the
    parseable structure (review nit)."""
    name = (name or "").replace("\r", " ").replace("\n", " ")
    for ch in ("*", "`", "#", "_", "[", "]"):
        name = name.replace(ch, "")
    return name.strip() or "Speaker ?"


# --------------------------------------------------------------------------- #
# Transcript renderers
# --------------------------------------------------------------------------- #
def transcript_md(meta: Any, records) -> str:
    parts = [_yaml_front_matter(meta, records), "", "## Transcript", ""]
    for rec in records:
        parts.append(f"**[{_fmt_hhmmss(rec.start)}] {_safe_speaker(rec.speaker)} ({rec.lang}):** {rec.text}")
        if getattr(rec, "translation", None):
            parts.append(f"> {rec.translation}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def transcript_txt(meta: Any, records) -> str:
    m = _meta_dict(meta)
    parts = [f"{m.get('title', 'meeting')} — {(m.get('created_at') or '')[:10]}", ""]
    for rec in records:
        parts.append(f"[{_fmt_hhmmss(rec.start)}] {_safe_speaker(rec.speaker)}: {rec.text}")
        if getattr(rec, "translation", None):
            parts.append(f"    {rec.translation}")
    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Summary renderers
# --------------------------------------------------------------------------- #
def summary_md(meta: Any, summary: str | None) -> str:
    body = summary if summary else "_No summary generated yet._"
    m = _meta_dict(meta)
    fm = [
        "---",
        f"session_id: {m.get('session_id', '')}",
        f"title: {_yaml_str(m.get('title', ''))}",
        f"date: {(m.get('created_at') or '')[:10]}",
        f"summary_scenario: {m.get('summary_scenario') or ''}",
        f"summary_provider: {m.get('summary_provider') or ''}",
        "---",
    ]
    return "\n".join(fm) + "\n\n" + body.rstrip() + "\n"


def summary_txt(meta: Any, summary: str | None) -> str:
    m = _meta_dict(meta)
    body = summary if summary else "No summary generated yet."
    # Strip the most common Markdown markers for a plain-text rendering.
    plain = _strip_markdown(body)
    header = f"{m.get('title', 'meeting')} — Summary"
    return header + "\n\n" + plain.rstrip() + "\n"


def _strip_markdown(text: str) -> str:
    out_lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        while stripped.startswith("#"):
            stripped = stripped[1:]
        stripped = stripped.lstrip("#").strip()
        for marker in ("**", "__", "`"):
            stripped = stripped.replace(marker, "")
        if stripped.startswith(("- ", "* ")):
            stripped = "- " + stripped[2:]
        out_lines.append(stripped)
    return "\n".join(out_lines)


# --------------------------------------------------------------------------- #
# Combined + dispatcher
# --------------------------------------------------------------------------- #
def combined_md(meta: Any, records, summary: str | None) -> str:
    m = _meta_dict(meta)
    summary_body = summary if summary else "_No summary generated yet._"
    parts = [
        _yaml_front_matter(meta, records),
        "",
        "## Summary",
        "",
        summary_body.rstrip(),
        "",
        "## Transcript",
        "",
    ]
    for rec in records:
        parts.append(f"**[{_fmt_hhmmss(rec.start)}] {_safe_speaker(rec.speaker)} ({rec.lang}):** {rec.text}")
        if getattr(rec, "translation", None):
            parts.append(f"> {rec.translation}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def combined_txt(meta: Any, records, summary: str | None) -> str:
    parts = [summary_txt(meta, summary).rstrip(), "", "-" * 40, "", transcript_txt(meta, records).rstrip()]
    return "\n".join(parts).rstrip() + "\n"


def render_export(session_data, what: str, fmt: str) -> tuple[str, str, str]:
    """Return ``(filename, content, media_type)`` for a session export (addendum §E3).

    ``what`` ∈ {transcript, summary, combined}; ``fmt`` ∈ {md, txt}.
    """
    if what not in WHAT_VALUES:
        raise ValueError(f"invalid what: {what!r}")
    if fmt not in FMT_VALUES:
        raise ValueError(f"invalid fmt: {fmt!r}")
    meta = session_data.meta
    records = session_data.utterances
    summary = session_data.summary
    sid = _meta_dict(meta).get("session_id", "session")

    if what == "transcript":
        content = transcript_md(meta, records) if fmt == "md" else transcript_txt(meta, records)
    elif what == "summary":
        content = summary_md(meta, summary) if fmt == "md" else summary_txt(meta, summary)
    else:  # combined
        content = combined_md(meta, records, summary) if fmt == "md" else combined_txt(meta, records, summary)

    filename = f"{sid}-{what}.{fmt}"
    media_type = "text/markdown; charset=utf-8" if fmt == "md" else "text/plain; charset=utf-8"
    return filename, content, media_type
