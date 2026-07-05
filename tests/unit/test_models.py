"""Local-summarizer model catalog: loader, default resolution, ollama list parsing."""

from __future__ import annotations

import ai_record.models as models
from ai_record.config import Settings, load_model_catalog


def test_catalog_loads_and_has_qwen_default():
    cat = load_model_catalog()
    assert cat["default"] == "qwen2.5:7b"
    tags = {m["tag"] for m in cat["models"]}
    assert "qwen2.5:7b" in tags
    # every catalog entry carries the curated fields
    for m in cat["models"]:
        assert {"tag", "family", "params", "vram_gb", "tier"} <= set(m)


def test_default_model_helper():
    assert models.default_model() == "qwen2.5:7b"


def test_config_ollama_model_default_is_qwen():
    assert Settings().ollama_model == "qwen2.5:7b"


def test_load_catalog_falls_back_when_file_unreadable(monkeypatch, tmp_path):
    # Point the loader at a nonexistent path → safe fallback, still qwen default.
    monkeypatch.setattr(models, "_CATALOG_PATH", tmp_path / "missing.json")
    cat = models.load_model_catalog()
    assert cat["default"] == "qwen2.5:7b"
    assert isinstance(cat["models"], list) and cat["models"]


def test_parse_ollama_list_skips_header_and_blanks():
    out = (
        "NAME              ID              SIZE      MODIFIED\n"
        "qwen2.5:7b        845dc...        4.7 GB    2 days ago\n"
        "\n"
        "llama3.1:8b       abc123...       4.9 GB    1 week ago\n"
    )
    assert models._parse_ollama_list(out) == ["qwen2.5:7b", "llama3.1:8b"]


def test_list_installed_models_empty_when_ollama_absent(monkeypatch):
    monkeypatch.setattr(models.shutil, "which", lambda _name: None)
    assert models.list_installed_models() == []
