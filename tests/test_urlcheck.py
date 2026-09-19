"""Tests for safe_http_url (GTK-free)."""

from __future__ import annotations

from helios.backend.urlcheck import safe_http_url

DEFAULT = "http://default.example:9101"


def test_accepts_valid_http_and_https():
    assert safe_http_url("http://host:1234", DEFAULT) == "http://host:1234"
    assert safe_http_url("https://host/path", DEFAULT) == "https://host/path"


def test_empty_or_none_falls_back():
    assert safe_http_url(None, DEFAULT) == DEFAULT
    assert safe_http_url("", DEFAULT) == DEFAULT


def test_rejects_non_http_schemes():
    assert safe_http_url("file:///etc/passwd", DEFAULT) == DEFAULT
    assert safe_http_url("gopher://host", DEFAULT) == DEFAULT
    assert safe_http_url("ftp://host", DEFAULT) == DEFAULT


def test_rejects_schemeless_or_garbage():
    assert safe_http_url("localhost:11434", DEFAULT) == DEFAULT  # no scheme
    assert safe_http_url("not a url", DEFAULT) == DEFAULT
    assert safe_http_url("http://", DEFAULT) == DEFAULT  # scheme but no host
