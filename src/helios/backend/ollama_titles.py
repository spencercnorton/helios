"""Small, bounded Ollama requests for title generation and readiness checks."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

from helios.backend.urlcheck import safe_http_url

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5-coder:14b"
MAX_RESPONSE_BYTES = 256 * 1024


def validate_config(base_url: str, model: str) -> tuple[str, str]:
    url = safe_http_url(base_url.strip(), "") if isinstance(base_url, str) else ""
    if not url or any(ord(char) < 32 for char in url):
        raise ValueError("Enter a complete http:// or https:// server URL.")
    parsed = urllib.parse.urlsplit(url)
    try:
        valid_host = bool(parsed.hostname) and (parsed.port is None or parsed.port > 0)
    except ValueError:
        valid_host = False
    if not valid_host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a server URL without credentials, query parameters or fragments.")
    tag = model.strip() if isinstance(model, str) else ""
    if not tag or len(tag) > 256 or any(char.isspace() or ord(char) < 32 for char in tag):
        raise ValueError("Enter an Ollama model name, such as qwen3.6:35b.")
    return urllib.parse.urlunsplit(parsed).rstrip("/"), tag


def _read_json(request: urllib.request.Request, *, timeout: float) -> dict:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Ollama response exceeded the size limit.")
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("Ollama returned an unexpected response.")
    return body


@dataclass(frozen=True)
class ModelReadiness:
    available: bool
    model: str


def check_model(base_url: str, model: str, *, timeout: float = 5.0) -> ModelReadiness:
    """Check installed models only: never load a model or generate text."""
    url, tag = validate_config(base_url, model)
    body = _read_json(urllib.request.Request(url + "/api/tags", method="GET"), timeout=timeout)
    rows = body.get("models")
    if not isinstance(rows, list):
        raise ValueError("The server did not return an Ollama model list.")
    names = {
        value for row in rows if isinstance(row, dict)
        for key in ("name", "model")
        if isinstance(value := row.get(key), str)
    }
    candidates = {tag}
    if ":" not in tag.rsplit("/", 1)[-1]:
        candidates.add(tag + ":latest")
    return ModelReadiness(bool(candidates & names), tag)


def generate_title(base_url: str, model: str, prompt: str, *, timeout: float = 30) -> str:
    url, tag = validate_config(base_url, model)
    payload = json.dumps({
        "model": tag,
        "prompt": prompt,
        "stream": False,
        # This is a short title, not reasoning work. On thinking-by-default
        # models, hidden reasoning can otherwise consume the entire 32 tokens.
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 32},
    }).encode("utf-8")
    request = urllib.request.Request(
        url + "/api/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    body = _read_json(request, timeout=timeout)
    text = body.get("response", "")
    if not isinstance(text, str):
        raise ValueError("Ollama returned an invalid title response.")
    return text.strip()
