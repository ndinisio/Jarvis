"""WhisperCppSTT: whisper.cpp support, and whether it's actually running on
the Neural Engine (a Core ML encoder next to the model) or falling back to
Metal — whisper.cpp's own binary decides which by looking for that sibling
file; JARVIS only detects and reports it, never invents the flag."""

from __future__ import annotations

import pytest
from jarvis.voice.stt import WhisperCppSTT, _coreml_encoder_path

pytestmark = pytest.mark.asyncio


def test_coreml_encoder_path_follows_whisper_cpps_own_naming():
    assert (str(_coreml_encoder_path("/models/ggml-base.en.bin"))
            == "/models/ggml-base.en-encoder.mlmodelc")


def test_coreml_encoder_path_is_none_for_a_non_ggml_path():
    assert _coreml_encoder_path("/models/some-other-file.gguf") is None
    assert _coreml_encoder_path("") is None


async def test_reports_metal_only_when_no_coreml_encoder_sits_beside_the_model(tmp_path, monkeypatch):
    model = tmp_path / "ggml-base.en.bin"
    model.write_bytes(b"not a real model, just needs to exist")

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/whisper-cli")
    stt = WhisperCppSTT(model_path=str(model))
    assert stt.coreml_active is False
    ok, note = await stt.available()
    assert ok is True
    assert "Metal only" in note


async def test_reports_the_core_ml_encoder_when_it_is_present(tmp_path, monkeypatch):
    model = tmp_path / "ggml-base.en.bin"
    model.write_bytes(b"not a real model, just needs to exist")
    encoder = tmp_path / "ggml-base.en-encoder.mlmodelc"
    encoder.mkdir()  # a compiled Core ML model is a directory, not a file

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/whisper-cli")
    stt = WhisperCppSTT(model_path=str(model))
    assert stt.coreml_active is True
    ok, note = await stt.available()
    assert ok is True
    assert "Core ML" in note and "Neural Engine" in note


async def test_unavailable_without_the_binary_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    stt = WhisperCppSTT(model_path="/does/not/matter")
    ok, note = await stt.available()
    assert ok is False
    assert "PATH" in note


async def test_unavailable_without_a_model_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/local/bin/whisper-cli")
    stt = WhisperCppSTT(model_path="")
    ok, note = await stt.available()
    assert ok is False
    assert "GGML" in note
