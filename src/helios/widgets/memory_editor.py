"""Editor widget for a single memory file.

Layout:

    ┌─ memory editor ──────────────────────────┐
    │ name:        [ slug-here              ]  │  frontmatter form
    │ description: [ one-line summary        ] │
    │ type:        [ user ▾]                   │
    │                                          │
    │ ┌─ body (markdown) ─────────────────────┐│
    │ │                                       ││  GtkSource.View
    │ │ Body text...                          ││
    │ │                                       ││
    │ └───────────────────────────────────────┘│
    │                                          │
    │ ● Unsaved changes    [Discard] [Save]    │  status + actions
    └──────────────────────────────────────────┘

Dirty tracking: any change to the frontmatter form or body buffer flips
us into "dirty" state, surfaces the Save/Discard action bar via a
`Gtk.Revealer`, and emits `dirty-changed(bool)` so the parent can
disable navigation if it wants.

External-change detection: if the on-disk file is modified out-of-band
between load and save, we surface an `Adw.Banner` letting the user pick
between "Reload" (lose changes) and "Save anyway" (clobber the external
edit). They never get silently overwritten.

Save uses `helios.backend.memory_io.save()` which writes atomically and
rotates backups (last 5 versions kept under `~/.helios/backups/`).

Plain mode (`set_file(path, plain=True)`) is for CLAUDE.md / MEMORY.md:
they carry no frontmatter, so the form above is hidden and the whole file
is the body, loaded/saved through `memory_io.load_plain()`/`save_plain()`
so a leading `---` line is never mistaken for a header.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GtkSource", "5")
from gi.repository import Adw, GObject, Gtk, GtkSource  # noqa: E402

from helios.backend.memory_io import (
    MemoryFile,
    has_external_changes,
    load,
    load_plain,
    save,
    save_plain,
)
from helios.widgets._motion import BASE_MS


# Conventional `metadata.type` values claude uses in our memory files.
KNOWN_TYPES = ["user", "feedback", "project", "reference"]


class MemoryEditor(Gtk.Box):
    """Edit a single memory file. Holds a `MemoryFile` internally and
    exposes signals for the parent pane to react to."""

    __gsignals__ = {
        # Emitted on every dirty/clean transition. True = dirty.
        "dirty-changed": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        # Emitted after a successful save.
        "saved": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("helios-memory-editor")

        self._mem: MemoryFile | None = None
        # True while editing CLAUDE.md / MEMORY.md — no frontmatter form,
        # load/save go through the *_plain entry points. Set by set_file().
        self._plain = False
        self._dirty = False
        # Block the dirty-detection signals while we programmatically load
        # values into the form during set_file().
        self._loading = False
        # True after a save attempt found the file changed on disk. The next
        # Save (relabelled "Save anyway") overwrites; Reload clears it. The
        # user is never locked out of saving their own edits.
        self._conflict = False

        # ── External-change banner ──
        self._ext_banner = Adw.Banner.new(
            "File changed on disk since we loaded it."
        )
        self._ext_banner.set_button_label("Reload")
        self._ext_banner.set_revealed(False)
        self._ext_banner.connect("button-clicked", self._on_reload_clicked)
        self.append(self._ext_banner)

        # ── Lossy-frontmatter banner ──
        # Files whose frontmatter our parser can't fully represent (lists,
        # deep nesting, …) are body-only editable; the original frontmatter
        # block is written back verbatim (see memory_io.MemoryFile.lossy).
        self._lossy_banner = Adw.Banner.new(
            "Complex frontmatter — body-only editing; the header is preserved verbatim."
        )
        self._lossy_banner.set_revealed(False)
        self.append(self._lossy_banner)

        # ── Frontmatter form ──
        form = Adw.PreferencesGroup()
        form.set_margin_top(8)
        form.set_margin_bottom(4)
        form.set_margin_start(12)
        form.set_margin_end(12)

        self._name_entry = Adw.EntryRow.new()
        self._name_entry.set_title("name")
        self._name_entry.connect("changed", self._on_field_changed)
        form.add(self._name_entry)

        self._desc_entry = Adw.EntryRow.new()
        self._desc_entry.set_title("description")
        self._desc_entry.connect("changed", self._on_field_changed)
        form.add(self._desc_entry)

        self._type_combo = Adw.ComboRow.new()
        self._type_combo.set_title("type")
        self._type_combo.set_subtitle("metadata.type")
        type_model = Gtk.StringList.new(KNOWN_TYPES + ["(other)"])
        self._type_combo.set_model(type_model)
        self._type_combo.connect("notify::selected", self._on_field_changed)
        form.add(self._type_combo)

        self._form = form
        self.append(form)

        # ── Body editor (GtkSource markdown) ──
        body_label = Gtk.Label(label="Body", xalign=0)
        body_label.add_css_class("caption-heading")
        body_label.add_css_class("dim-label")
        body_label.set_margin_start(16)
        body_label.set_margin_top(12)
        body_label.set_margin_bottom(4)
        self.append(body_label)

        self._buffer = GtkSource.Buffer.new(None)
        md_lang = GtkSource.LanguageManager.get_default().get_language("markdown")
        if md_lang is not None:
            self._buffer.set_language(md_lang)
        self._buffer.set_highlight_syntax(True)
        # Use the same Adwaita scheme as code blocks pick up — and keep
        # following dark/light flips. One long-lived editor instance per
        # window, so a direct connection can't accumulate (unlike the
        # per-CodeBlock leak fixed in 3B.5).
        self._apply_scheme()
        Adw.StyleManager.get_default().connect(
            "notify::dark", lambda *_: self._apply_scheme()
        )
        self._buffer.connect("changed", self._on_field_changed)

        self._view = GtkSource.View.new_with_buffer(self._buffer)
        self._view.set_monospace(False)  # markdown reads better in proportional
        self._view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view.set_top_margin(10)
        self._view.set_bottom_margin(10)
        self._view.set_left_margin(14)
        self._view.set_right_margin(14)
        self._view.set_show_line_numbers(False)
        self._view.set_pixels_above_lines(2)
        self._view.set_pixels_inside_wrap(2)
        self._view.add_css_class("helios-memory-body")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._view)
        scroller.set_margin_start(12)
        scroller.set_margin_end(12)
        scroller.set_margin_top(4)
        scroller.add_css_class("helios-memory-bodywrap")
        self.append(scroller)

        # ── Action bar (Save / Discard) ──
        self._action_revealer = Gtk.Revealer()
        self._action_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._action_revealer.set_transition_duration(BASE_MS)
        self._action_revealer.set_reveal_child(False)

        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_bar.set_margin_start(12)
        action_bar.set_margin_end(12)
        action_bar.set_margin_top(8)
        action_bar.set_margin_bottom(10)
        action_bar.add_css_class("helios-memory-actionbar")

        # ● Unsaved changes — small dot + label
        self._dirty_dot = Gtk.Box()
        self._dirty_dot.set_size_request(8, 8)
        self._dirty_dot.set_valign(Gtk.Align.CENTER)
        self._dirty_dot.add_css_class("helios-memory-dirty-dot")
        action_bar.append(self._dirty_dot)
        self._dirty_label = Gtk.Label(label="Unsaved changes", xalign=0)
        self._dirty_label.add_css_class("caption")
        self._dirty_label.add_css_class("dim-label")
        self._dirty_label.set_hexpand(True)
        self._dirty_label.set_halign(Gtk.Align.START)
        action_bar.append(self._dirty_label)

        discard_btn = Gtk.Button.new_with_label("Discard")
        discard_btn.add_css_class("flat")
        discard_btn.connect("clicked", self._on_discard_clicked)
        action_bar.append(discard_btn)

        self._save_btn = Gtk.Button.new_with_label("Save")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._on_save_clicked)
        action_bar.append(self._save_btn)

        self._action_revealer.set_child(action_bar)
        self.append(self._action_revealer)

    # ── Public API ────────────────────────────────────────────────────

    def set_file(self, path, *, plain: bool = False) -> None:
        """Load `path` from disk into the editor. Unsaved changes are lost —
        the parent pane (ContextPane) is responsible for prompting first.

        `plain=True` is for CLAUDE.md / MEMORY.md: they have no frontmatter,
        so the form stays hidden and the whole file loads as the body via
        memory_io.load_plain() — a leading `---` line is never parsed."""
        self._plain = plain
        self._loading = True
        try:
            self._mem = load_plain(path) if plain else load(path)
            self._form.set_visible(not plain)
            if not plain:
                self._name_entry.set_text(self._mem.name)
                self._desc_entry.set_text(self._mem.description)
                t = (
                    self._mem.frontmatter.get("metadata", {}).get("type")
                    if isinstance(self._mem.frontmatter.get("metadata"), dict)
                    else None
                )
                try:
                    self._type_combo.set_selected(KNOWN_TYPES.index(t) if t in KNOWN_TYPES else len(KNOWN_TYPES))
                except (ValueError, AttributeError):
                    self._type_combo.set_selected(len(KNOWN_TYPES))
            self._buffer.set_text(self._mem.body or "")
            # Lossy frontmatter → the form can't faithfully edit it; lock the
            # form rows and write the original header back verbatim on save.
            # (Always False in plain mode: load_plain() never parses one.)
            lossy = self._mem.lossy
            self._name_entry.set_sensitive(not lossy)
            self._desc_entry.set_sensitive(not lossy)
            self._type_combo.set_sensitive(not lossy)
            self._lossy_banner.set_revealed(lossy)
        finally:
            self._loading = False
        self._set_dirty(False)
        self._clear_conflict()
        self._ext_banner.set_revealed(False)

    def file_path(self):
        return self._mem.path if self._mem else None

    def is_dirty(self) -> bool:
        return self._dirty

    def commit(self) -> None:
        """Save current form values into the in-memory MemoryFile and write."""
        if self._mem is None:
            return
        # Detect external changes before clobbering them. First attempt arms
        # the conflict state instead of saving; the button relabels to
        # "Save anyway" so a second click overwrites deliberately. (The old
        # behavior just early-returned — the user's edits were unsavable.)
        if has_external_changes(self._mem) and not self._conflict:
            self._conflict = True
            self._ext_banner.set_revealed(True)
            self._save_btn.set_label("Save anyway")
            self._dirty_label.set_label(
                "Changed on disk — Reload to take theirs, Save anyway to keep yours."
            )
            return

        # Sync form → MemoryFile. Plain files (no form at all) and lossy
        # files (form locked, original frontmatter kept verbatim) both skip
        # this — only the body applies.
        if not self._plain and not self._mem.lossy:
            self._mem.name = self._name_entry.get_text().strip()
            self._mem.description = self._desc_entry.get_text().strip()
            # frontmatter dict — keep existing extras (we don't surface them
            # in the form) but update the ones we control.
            fm = dict(self._mem.frontmatter or {})
            fm["name"] = self._mem.name
            if self._mem.description:
                fm["description"] = self._mem.description
            else:
                fm.pop("description", None)
            # metadata.type
            idx = self._type_combo.get_selected()
            meta = dict(fm.get("metadata") or {}) if isinstance(fm.get("metadata"), dict) else {}
            if 0 <= idx < len(KNOWN_TYPES):
                meta["type"] = KNOWN_TYPES[idx]
                fm["metadata"] = meta
            elif idx == len(KNOWN_TYPES) and not meta.get("type"):
                # "(other)" with no existing type — leave it out
                pass

            self._mem.frontmatter = fm

        start, end = self._buffer.get_bounds()
        self._mem.body = self._buffer.get_text(start, end, True)

        try:
            (save_plain if self._plain else save)(self._mem, backup=True)
            self._set_dirty(False)
            self._clear_conflict()
            self._ext_banner.set_revealed(False)
            self.emit("saved", self._mem)
        except OSError as e:
            self._dirty_label.set_label(f"Save failed: {e}")
            # leave the action bar revealed; user can try again

    # ── Internals ────────────────────────────────────────────────────

    def _apply_scheme(self) -> None:
        sm = Adw.StyleManager.get_default()
        scheme_mgr = GtkSource.StyleSchemeManager.get_default()
        name = "Adwaita-dark" if sm.get_dark() else "Adwaita"
        scheme = scheme_mgr.get_scheme(name)
        if scheme is not None:
            self._buffer.set_style_scheme(scheme)

    def _clear_conflict(self) -> None:
        self._conflict = False
        self._save_btn.set_label("Save")

    def _on_field_changed(self, *_args) -> None:
        if self._loading:
            return
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        if dirty == self._dirty:
            return
        self._dirty = dirty
        self._action_revealer.set_reveal_child(dirty)
        # Reset label after a failed save's message
        if dirty:
            self._dirty_label.set_label("Unsaved changes")
        self.emit("dirty-changed", dirty)

    def _on_save_clicked(self, *_args) -> None:
        self.commit()

    def _on_discard_clicked(self, *_args) -> None:
        if self._mem is None:
            return
        # Reload from disk = discard
        self.set_file(self._mem.path, plain=self._plain)

    def _on_reload_clicked(self, *_args) -> None:
        if self._mem is not None:
            self.set_file(self._mem.path, plain=self._plain)
            self._ext_banner.set_revealed(False)
