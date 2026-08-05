# tests/test_voice_provider.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from receptionist.agent import _build_google_realtime_model_kwargs
from receptionist.config import VoiceConfig


def test_provider_defaults_to_openai():
    cfg = VoiceConfig()
    assert cfg.provider == "openai"


def test_provider_accepts_google():
    cfg = VoiceConfig(provider="google")
    assert cfg.provider == "google"


def test_provider_rejects_unknown():
    with pytest.raises(ValidationError):
        VoiceConfig(provider="anthropic")


def test_google_kwargs_substitute_openai_defaults():
    """OpenAI-flavored YAML defaults must not leak into the Gemini call:
    the default model is omitted (plugin default applies) and "marin"
    maps to "Puck"."""
    cfg = VoiceConfig(provider="google")
    kwargs = _build_google_realtime_model_kwargs(cfg)
    assert "model" not in kwargs
    assert kwargs["voice"] == "Puck"


def test_google_kwargs_pass_through_explicit_values():
    cfg = VoiceConfig(
        provider="google",
        voice_id="Kore",
        model="gemini-2.5-flash-native-audio-preview-12-2025",
    )
    kwargs = _build_google_realtime_model_kwargs(cfg)
    assert kwargs["model"] == "gemini-2.5-flash-native-audio-preview-12-2025"
    assert kwargs["voice"] == "Kore"
