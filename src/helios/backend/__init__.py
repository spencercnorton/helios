"""Backend.

The package root is the pure-data + filesystem layer:
  * `projects`, `transcript`, `memory`, `session_state`, `project_names`,
    `claude_binary` — no GTK imports, importable from any thread.

GLib/Gio-using subprocess wrappers live under `helios.backend.process`:
  * `process.cli_driver`     — drives the `claude` CLI in stream-json mode
  * `process.title_generator` — async LLM title generator with cache

`backend.process` modules require GTK4 + PyGObject because they integrate
with the GLib main loop for async subprocess I/O.
"""
