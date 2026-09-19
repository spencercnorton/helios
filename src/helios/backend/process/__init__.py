"""Subprocess drivers for the two chat backends.

`cli_driver` (claude) and `codex_driver` (OpenAI Codex) require GTK4 +
PyGObject — they integrate with GLib's main loop for async I/O. Their
shared shapes and wire-protocol translation live in `streaming` and
`codex_events`, which are deliberately GTK-free so the slim CI image can
unit-test them. Everything else in `helios.backend` is pure data +
filesystem, importable from any thread.
"""
