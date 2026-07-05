"""M4 export: md front-matter + txt round-trip + all-utterances-present + combined."""

from __future__ import annotations

import pytest

from ai_record.export import render_export
from tests.unit.test_store import _rec


def _session(store, title="Standup"):
    sess = store.create(title)
    sid = sess.session_id
    store.append_utterance(_rec(store, sid, text="t0", start=0.0))
    store.append_utterance(_rec(store, sid, text="t1", start=2.0))
    store.append_utterance(_rec(store, sid, text="t2", start=4.0))
    store.patch_utterance(sid, 1, {"translation": "xin chào"})
    return sid


def test_transcript_md_has_front_matter_and_translation(store):
    sid = _session(store)
    data = store.load_session(sid)
    fn, content, media = render_export(data, "transcript", "md")
    assert fn == f"{sid}-transcript.md"
    assert content.startswith("---")
    assert "session_id:" in content and "languages:" in content
    assert "> xin chào" in content
    assert "text/markdown" in media


def test_transcript_txt_is_plain_and_roundtrips(store):
    sid = _session(store)
    data = store.load_session(sid)
    fn, content, media = render_export(data, "transcript", "txt")
    assert "t0" in content and "t1" in content and "t2" in content
    assert "xin chào" in content
    assert "**" not in content  # no markdown markup
    assert "text/plain" in media


def test_export_contains_all_utterances(store):
    sid = _session(store)
    data = store.load_session(sid)
    _, content, _ = render_export(data, "transcript", "md")
    for t in ("t0", "t1", "t2"):
        assert t in content


def test_combined_export_has_summary_and_transcript(store):
    sid = _session(store)
    store.write_summary(sid, "SUMMARY BODY", scenario="minutes", provider="fake")
    data = store.load_session(sid)
    _, content, _ = render_export(data, "combined", "md")
    assert "## Summary" in content and "SUMMARY BODY" in content
    assert "## Transcript" in content and "t0" in content


def test_invalid_export_args_raise(store):
    sid = _session(store)
    data = store.load_session(sid)
    with pytest.raises(ValueError):
        render_export(data, "bogus", "md")
    with pytest.raises(ValueError):
        render_export(data, "transcript", "pdf")
