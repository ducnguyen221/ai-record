import json

import pytest

from ai_record.config import (
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
