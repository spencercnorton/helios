"""Ollama title requests are bounded; readiness never invokes a model."""

import io
import json

import pytest

from helios.backend import ollama_titles as ollama


def respond(monkeypatch, data):
    calls = []

    class Response(io.BytesIO):
        def read(self, size=-1):
            assert size == ollama.MAX_RESPONSE_BYTES + 1
            return super().read(size)

    def open_request(request, *, timeout):
        calls.append((request, timeout))
        return Response(data if isinstance(data, bytes) else json.dumps(data).encode())

    monkeypatch.setattr(ollama.urllib.request, "urlopen", open_request)
    return calls


def test_thinking_model_gets_answer_budget_and_bounded_response(monkeypatch):
    calls = respond(monkeypatch, {"response": "  Inspect scheduler capacity  ", "done": True})
    assert ollama.generate_title("http://server:11434/", "qwen3.6:35b", "A synthetic title") == "Inspect scheduler capacity"
    request, timeout = calls[0]
    payload = json.loads(request.data)
    assert request.full_url == "http://server:11434/api/generate"
    assert request.method == "POST" and timeout == 30
    assert payload["think"] is False
    assert payload["stream"] is False and payload["options"]["num_predict"] == 32


@pytest.mark.parametrize("model,available", [("qwen3.6:35b", True), ("missing", False), ("gemma4", True)])
def test_readiness_uses_only_get_tags(monkeypatch, model, available):
    calls = respond(monkeypatch, {"models": [{"name": "qwen3.6:35b"}, {"model": "gemma4:latest"}]})
    status = ollama.check_model("http://server:11434", model)
    assert status.available is available
    request, timeout = calls[0]
    assert request.full_url == "http://server:11434/api/tags"
    assert request.method == "GET" and request.data is None and timeout == 5


@pytest.mark.parametrize("url", ["file:///secret", "localhost:11434", "http://host:bad", "http://u:" + "p@host", "http://host?token=x"])
def test_invalid_configuration_makes_no_request(monkeypatch, url):
    calls = respond(monkeypatch, {})
    with pytest.raises(ValueError):
        ollama.check_model(url, "model")
    assert calls == []


def test_oversize_and_malformed_responses_are_rejected(monkeypatch):
    respond(monkeypatch, b"x" * (ollama.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(ValueError, match="size limit"):
        ollama.check_model("http://server", "model")
    respond(monkeypatch, {"models": "unexpected"})
    with pytest.raises(ValueError, match="model list"):
        ollama.check_model("http://server", "model")
    respond(monkeypatch, {"response": ["not a title"]})
    with pytest.raises(ValueError, match="invalid title"):
        ollama.generate_title("http://server", "model", "prompt")
