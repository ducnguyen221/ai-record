import json

import pytest

from ai_record.config import (
    DEFAULT_SUMMARY_SCENARIOS,
    PRESETS,
    Secrets,
    Settings,
    detect_preset_name,
    resolve_preset,
)


def test_detect_preset_name():
    assert detect_preset_name(None) == "cpu"
    assert detect_preset_name(8) == "gpu_8gb"
    assert detect_preset_name(12) == "gpu_12gb"
    assert detect_preset_name(24) == "gpu_16gb_plus"


def test_resolve_preset_default_and_override():
    s = Settings(hardware_preset="gpu_12gb")
    p = resolve_preset(s)
    assert p.name == "gpu_12gb"
    assert p.whisper_model == "large-v3"
    assert p.whisper_compute_type == "int8_float16"
    assert p.translation_device == "cpu"

    s2 = Settings(hardware_preset="gpu_12gb", whisper_model="small")
    assert resolve_preset(s2).whisper_model == "small"


def test_beam_modes():
    p = PRESETS["gpu_12gb"]
    assert p.beam("fast") == 1
    assert p.beam("quality") == 5


def test_load_save_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(server_port=9001, translate_enabled=True)
    s.save(path)
    loaded = Settings.load(path)
    assert loaded.server_port == 9001
    assert loaded.translate_enabled is True


def test_unknown_key_tolerated():
    s = Settings.from_dict({"server_port": 9002, "not_a_real_key": 123})
    assert s.server_port == 9002


def test_validator_rejects_bad_enum():
    with pytest.raises(ValueError):
        Settings(hardware_preset="bogus")


def test_redacted_never_leaks_secret_values():
    sec = Secrets()
    sec.set("gemini_api_key", "super-secret-value")
    try:
        red = Settings().redacted(sec)
        assert "gemini_api_key" not in red
        assert "hf_token" not in red
        assert red["gemini_api_key_is_set"] is True
        assert red["hf_token_is_set"] is False
        assert "super-secret-value" not in json.dumps(red)
    finally:
        sec.clear("gemini_api_key")


def test_secrets_set_get_clear():
    sec = Secrets()
    assert sec.is_set("hf_token") is False
    sec.set("hf_token", "tok")
    assert sec.get("hf_token") == "tok"
    assert sec.is_set("hf_token") is True
    sec.clear("hf_token")
    assert sec.is_set("hf_token") is False


def test_secrets_reject_unknown_name():
    with pytest.raises(ValueError):
        Secrets().set("aws_key", "x")


def test_acknowledge_consent():
    s = Settings().acknowledge_consent()
    assert s.consent_acknowledged is True
    assert s.consent_acknowledged_at is not None


# --------------------------------------------------------------------------- #
# Output-file settings (Feature 2)
# --------------------------------------------------------------------------- #
def test_output_settings_defaults():
    s = Settings()
    assert s.keep_audio is False
    assert s.audio_export_format == "mp3"
    assert s.save_txt is False


def test_output_settings_put_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings().update({"keep_audio": True, "audio_export_format": "wav", "save_txt": True})
    s.save(path)
    loaded = Settings.load(path)
    assert loaded.keep_audio is True
    assert loaded.audio_export_format == "wav"
    assert loaded.save_txt is True


def test_output_settings_bad_format_rejected():
    with pytest.raises(ValueError):
        Settings(audio_export_format="flac")


def test_output_settings_appear_in_redacted():
    red = Settings(keep_audio=True, save_txt=True).redacted(Secrets())
    assert red["keep_audio"] is True
    assert red["save_txt"] is True
    assert red["audio_export_format"] == "mp3"
    # redaction still only exposes secret booleans, never values.
    assert "hf_token" not in red and "gemini_api_key" not in red


# --------------------------------------------------------------------------- #
# output_formats multi-select (new feature)
# --------------------------------------------------------------------------- #
def test_output_formats_default_is_md_only():
    assert Settings().output_formats == ["md"]


def test_output_formats_put_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings().update({"output_formats": ["md", "txt", "summary"]})
    s.save(path)
    loaded = Settings.load(path)
    assert loaded.output_formats == ["md", "txt", "summary"]


def test_output_formats_ignores_unknowns_and_forces_md():
    # Unknown items dropped; "md" always present even when omitted.
    s = Settings(output_formats=["txt", "bogus", "mp3"])
    assert s.output_formats == ["md", "txt", "mp3"]
    assert Settings(output_formats=[]).output_formats == ["md"]


def test_output_formats_dedupes_preserving_order():
    s = Settings(output_formats=["mp3", "mp3", "md", "txt"])
    assert s.output_formats == ["mp3", "md", "txt"]


def test_output_formats_in_redacted_and_no_secret_leak():
    red = Settings(output_formats=["md", "summary"]).redacted(Secrets())
    assert red["output_formats"] == ["md", "summary"]
    assert "hf_token" not in red and "gemini_api_key" not in red


# --------------------------------------------------------------------------- #
# Editable Summary / Analyze prompts (summary_scenarios)
# --------------------------------------------------------------------------- #
def test_default_summary_prompts_present_and_nonempty():
    for key in ("reformat", "analyze"):
        assert key in DEFAULT_SUMMARY_SCENARIOS
        assert DEFAULT_SUMMARY_SCENARIOS[key].strip()
    # reformat must keep the verbatim contract; analyze must be a real analysis.
    assert "VERBATIM" in DEFAULT_SUMMARY_SCENARIOS["reformat"]
    assert "Tổng quan" in DEFAULT_SUMMARY_SCENARIOS["analyze"]


def test_settings_include_default_scenarios():
    s = Settings()
    assert s.summary_scenarios["reformat"] == DEFAULT_SUMMARY_SCENARIOS["reformat"]
    assert s.summary_scenarios["analyze"] == DEFAULT_SUMMARY_SCENARIOS["analyze"]
    # All catalog scenarios are seeded, not just the two editable ones.
    for key in ("minutes", "study_notes", "action_tracker", "article"):
        assert key in s.summary_scenarios


def test_summary_scenarios_put_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    edited = dict(DEFAULT_SUMMARY_SCENARIOS)
    edited["reformat"] = "My custom verbatim prompt."
    edited["analyze"] = "My custom analysis prompt."
    s = Settings().update({"summary_scenarios": edited})
    s.save(path)
    loaded = Settings.load(path)
    assert loaded.summary_scenarios["reformat"] == "My custom verbatim prompt."
    assert loaded.summary_scenarios["analyze"] == "My custom analysis prompt."
    # Untouched scenarios survive the merge round-trip.
    assert loaded.summary_scenarios["minutes"] == DEFAULT_SUMMARY_SCENARIOS["minutes"]


def test_summary_scenarios_in_redacted_no_secret_leak():
    red = Settings().redacted(Secrets())
    assert "summary_scenarios" in red
    assert red["summary_scenarios"]["reformat"] == DEFAULT_SUMMARY_SCENARIOS["reformat"]
    assert "hf_token" not in red and "gemini_api_key" not in red
