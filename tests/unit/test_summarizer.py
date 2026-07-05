"""M4 summarizer: scenario selection, reformat integrity guard + deterministic fallback,
and CLI prompt-injection hardening (stdin-only, no shell, delimiters, isolated cwd)."""

from __future__ import annotations

import pytest

import ai_record.summarizer as sm
from ai_record.config import Settings
from ai_record.summarizer import ClaudeCliSummarizer, build_summary
from tests.unit.test_store import _rec


class FakeProvider:
    name = "fakeprov"

    def __init__(self, out_fn=None) -> None:
        self.out_fn = out_fn
        self.calls: list[tuple[str, str]] = []

    def available(self):
        return True, ""

    def summarize(self, prompt, transcript_text, meta):
        self.calls.append((prompt, transcript_text))
        return self.out_fn(prompt, transcript_text) if self.out_fn else transcript_text


def _session(store, texts):
    sess = store.create("sum")
    sid = sess.session_id
    for i, t in enumerate(texts):
        store.append_utterance(_rec(store, sid, text=t, start=float(i * 2)))
    return store.load_session(sid)


# --------------------------------------------------------------------------- #
# scenario selection
# --------------------------------------------------------------------------- #
def test_scenario_selects_correct_prompt(store):
    data = _session(store, ["alpha", "beta"])
    fp = FakeProvider()
    build_summary(data, "minutes", "claude_cli", store.settings, provider_impl=fp)
    assert "meeting minutes" in fp.calls[0][0].lower()

    fp2 = FakeProvider()
    build_summary(data, "action_tracker", "claude_cli", store.settings, provider_impl=fp2)
    assert "action items" in fp2.calls[0][0].lower()


def test_unknown_scenario_raises(store):
    data = _session(store, ["alpha"])
    with pytest.raises(ValueError):
        build_summary(data, "nope", "claude_cli", store.settings, provider_impl=FakeProvider())


# --------------------------------------------------------------------------- #
# reformat integrity guard
# --------------------------------------------------------------------------- #
def test_reformat_no_fallback_when_text_verbatim(store):
    data = _session(store, ["hello world", "second line"])
    # Provider echoes the transcript (structured) → every utterance present verbatim.
    fp = FakeProvider(out_fn=lambda p, t: "## Topic\n" + t)
    res = build_summary(data, "reformat", "claude_cli", store.settings, provider_impl=fp)
    assert res.reformat_fallback is False
    assert res.scenario == "reformat"
    assert "hello world" in res.markdown


def test_reformat_falls_back_when_text_altered(store):
    data = _session(store, ["hello world", "second line"])
    # Provider mangles the text → integrity guard rejects → deterministic reformat.
    fp = FakeProvider(out_fn=lambda p, t: "## Topic\nCOMPLETELY DIFFERENT")
    res = build_summary(data, "reformat", "claude_cli", store.settings, provider_impl=fp)
    assert res.reformat_fallback is True
    # Deterministic path preserves every original utterance verbatim, zero alteration.
    assert "hello world" in res.markdown
    assert "second line" in res.markdown


def test_reformat_deterministic_needs_no_provider_call_content(store):
    """Even a provider that returns junk yields a lossless deterministic result."""
    data = _session(store, ["one two three"])
    fp = FakeProvider(out_fn=lambda p, t: "garbage")
    res = build_summary(data, "reformat", "claude_cli", store.settings, provider_impl=fp)
    assert res.reformat_fallback is True
    assert "one two three" in res.markdown


# --------------------------------------------------------------------------- #
# CLI hardening (SPEC.md §5.6)
# --------------------------------------------------------------------------- #
def test_claude_cli_hardening(monkeypatch):
    captured: dict = {}

    class _Result:
        returncode = 0
        stdout = "SUMMARY OK"
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _Result()

    monkeypatch.setattr(sm.subprocess, "run", fake_run)
    prov = ClaudeCliSummarizer(Settings(hardware_preset="cpu"))
    out = prov.summarize("MY_PROMPT", "the raw transcript text", {})
    assert out == "SUMMARY OK"

    cmd, kw = captured["cmd"], captured["kwargs"]
    # stdin-only: transcript + prompt travel via input=, never as argv.
    assert "the raw transcript text" in kw["input"]
    assert "MY_PROMPT" in kw["input"]
    assert not any("the raw transcript text" in str(a) for a in cmd)
    # never shell=True
    assert kw["shell"] is False
    # untrusted-data delimiters + system instruction present
    assert sm._BEGIN in kw["input"] and sm._END in kw["input"]
    # no-tools print mode
    assert "-p" in cmd and "--permission-mode" in cmd and "deny" in cmd
    assert "--allowedTools" in cmd
    # isolated cwd + CREATE_NO_WINDOW + minimal env
    assert kw["cwd"]
    assert "creationflags" in kw
    assert "env" in kw
