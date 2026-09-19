"""Helios settings — a multi-page Adw.PreferencesDialog.

Three surfaces, opened from the header's gear button:

  * **Account** — who's signed in (live `claude auth status`), with buttons to
    sign in / switch account and sign out. The sign-in flow is the official
    `claude auth login`, opened in a terminal.
  * **Defaults** — the default model plus the permission fallback used only
    for workspaces that do not have their own saved permission choice.
  * **Tools** — every MCP server (live `claude mcp list`, with health) plus the
    built-in Claude Code tools captured from the last session's init event.

Network/subprocess work (`claude auth status`, `claude mcp list`) runs on a
worker thread; results are marshalled back with GLib.idle_add and guarded so a
late callback can't touch a closed dialog.
"""

from __future__ import annotations

import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from helios import APP_ID
from helios.backend import claude_env, codex_env, model_catalog
from helios.backend import ollama_titles
from helios.backend.process.codex_app_hub import (
    CodexCredentialUpdateInUseError,
    get_shared_hub,
)
from helios.backend.project_perms import (
    GLOBAL_DEFAULT_MODES,
    PERMISSION_MODE_DESCRIPTORS,
    is_home_cwd,
)
from helios.backend.router_client import RouterClient
from helios.backend.ui_state import (
    DEFAULT_CWD_KEY,
    OPENROUTER_PICKER_KEY,
    default_cwd,
    store as ui_state_store,
)
from helios.log import log_file_path

# The shortlist list renders eagerly; several hundred Adw rows in one pass is a
# visible hitch. Anything past this needs the filter box, which is right there.
_OR_PICKER_ROW_CAP = 80


# The GLOBAL default picker. Every mode — including Bypass (full agentic
# access) — is selectable as the default; the picker flags Bypass with a
# warning. Corrupt/unknown state still falls back to Ask (see
# project_perms.sanitize_global_default), a persisted Bypass needs explicit
# confirmation at startup, legacy workspace Bypass stays retired to Ask, and a
# unknown providers still narrow global Bypass to Ask.
PERMISSION_MODES: list[tuple[str, str, str]] = [
    (mode.key, mode.label, mode.description)
    for mode in PERMISSION_MODE_DESCRIPTORS
    if mode.key in GLOBAL_DEFAULT_MODES
]

_DEFAULT_PERMISSION_SCOPE = "Starting fallback for unconfigured conversations."


_HEALTH_ICON: dict[str, tuple[str, str]] = {
    "connected": ("object-select-symbolic", "success"),
    "needs_auth": ("dialog-warning-symbolic", "warning"),
    "failed": ("dialog-error-symbolic", "error"),
    "unknown": ("dialog-question-symbolic", "dim-label"),
}

_ROUTER_AWAITING_STAGES = frozenset(
    {
        "manual_evaluation",
        "quarantined",
        "contract_probed",
        "shadow",
    }
)


def _router_profile_summary(profiles: list[dict]) -> str:
    if not profiles:
        return "No route profiles are currently available."
    counts: dict[str, int] = {}
    dispatchable = 0
    for profile in profiles:
        stage = str(profile.get("stage") or "unknown").replace("_", " ")
        counts[stage] = counts.get(stage, 0) + 1
        dispatchable += bool(profile.get("dispatchable", False))
    ordered = [
        f"{count} {stage}"
        for stage, count in sorted(counts.items())
    ]
    return f"{dispatchable} dispatchable · " + " · ".join(ordered)


def _router_awaiting_count(profiles: list[dict]) -> int:
    return sum(
        str(profile.get("stage") or "") in _ROUTER_AWAITING_STAGES
        for profile in profiles
    )


def _router_unavailable_subtitle(last_enabled: bool | None) -> str:
    if last_enabled is None:
        return (
            "Current preview state is unknown. Delegation fails closed; "
            "Claude and GPT continue normally."
        )
    state = "on" if last_enabled else "off"
    return (
        f"Current preview state is unknown; last confirmed {state}. "
        "Delegation fails closed."
    )


class SettingsDialog(Adw.PreferencesDialog):
    """Settings surface. Emits `apply(permission_mode, model)` on Save,
    `pool-visibility-changed` when the shared-pool toggle flips (so the
    window can reload the session list immediately). Credential mutation uses
    a durable callback because the dialog may close before its worker ends."""

    __gsignals__ = {
        "apply": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "pool-visibility-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(
        self,
        permission_mode: str,
        model: str,
        *,
        on_codex_credentials_changed: Callable[[], None] | None = None,
        on_openrouter_credentials_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.set_title("Helios Settings")
        self.set_content_width(620)
        self.set_content_height(640)

        self._closed = False
        self._codex_credentials_changed = on_codex_credentials_changed
        self._openrouter_credentials_changed = on_openrouter_credentials_changed
        self._codex_generation = 0
        self._codex_update_running = False
        self._router_syncing = False
        self._router_generation = 0
        self._router_mutating = False
        self._router_last_enabled: bool | None = None
        self._ollama_check_generation = 0
        self._ollama_check_busy = False
        self.connect("closed", self._on_closed)

        # Start with cheap fallback/current rows so the dialog opens without
        # blocking on binary/App Server work. The one Codex worker below loads
        # both provider status and the full picker catalog.
        self._selected_model_id = model
        self._model_choices: list[tuple[str, str]] = self._initial_model_choices(model)

        self._build_settings_page(permission_mode, model)
        self._build_providers_page()
        self._build_tools_page()

        # Kick off the live loads as soon as we're shown.
        self._reload_account()
        self._reload_mcp()
        self._reload_codex_mcp()
        self._reload_codex()
        self._reload_router()
        self._reload_openrouter()
        self._reload_openrouter()

    @staticmethod
    def _entry_pairs(entries: list[model_catalog.ModelEntry]) -> list[tuple[str, str]]:
        return [(e.id, e.label) for e in entries]

    @staticmethod
    def _selectable_openai_models(
        models: list[model_catalog.ModelEntry],
        status: str,
    ) -> list[model_catalog.ModelEntry]:
        """Return only GPT rows proven by the current App Server account.

        ``chatgpt-fallback`` rows are useful catalog diagnostics, but they are
        curated rather than account-bound and therefore cannot authorize a
        selectable model.
        """
        return list(models) if status == "app-server" else []

    def _initial_model_choices(self, selected_model: str) -> list[tuple[str, str]]:
        choices = self._entry_pairs(list(model_catalog.FALLBACK_ANTHROPIC))
        if (
            selected_model
            and model_catalog.provider_for(selected_model)
            != model_catalog.PROVIDER_OPENAI
            and all(model_id != selected_model for model_id, _label in choices)
        ):
            choices.insert(0, (selected_model, selected_model))
        return choices

    def _set_model_choices(self, choices: list[tuple[str, str]]) -> None:
        if not choices:
            choices = self._initial_model_choices(self._selected_model_id)
        if self._selected_model_id and all(
            model_id != self._selected_model_id for model_id, _label in choices
        ):
            if (
                model_catalog.provider_for(self._selected_model_id)
                == model_catalog.PROVIDER_OPENAI
            ):
                # Never resurrect a persisted GPT id that the authoritative
                # catalog did not supply. Select a safe visible row instead.
                self._selected_model_id = choices[0][0]
            else:
                choices = [(self._selected_model_id, self._selected_model_id), *choices]
        self._model_choices = choices
        model_model = Gtk.StringList()
        for _key, label in self._model_choices:
            model_model.append(label)
        self._model_row.set_model(model_model)
        midx = next(
            (
                i
                for i, (model_id, _label) in enumerate(self._model_choices)
                if model_id == self._selected_model_id
            ),
            0,
        )
        self._model_row.set_selected(midx)
        self._model_row.set_subtitle("Default model for new chats")

    # ── page 1: account + defaults ──────────────────────────────────────

    def _build_settings_page(self, permission_mode: str, model: str) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Settings")
        page.set_icon_name("emblem-system-symbolic")
        self.add(page)

        # --- Account -----------------------------------------------------
        self._account_group = Adw.PreferencesGroup()
        self._account_group.set_title("Account")
        page.add(self._account_group)

        refresh_acct = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_acct.add_css_class("flat")
        refresh_acct.set_tooltip_text("Refresh account status")
        refresh_acct.set_valign(Gtk.Align.CENTER)
        refresh_acct.connect("clicked", lambda *_: self._reload_account())
        self._account_group.set_header_suffix(refresh_acct)

        self._account_row = Adw.ActionRow()
        self._account_row.set_title("Checking…")
        self._account_icon = Gtk.Image.new_from_icon_name("avatar-default-symbolic")
        self._account_icon.add_css_class("dim-label")
        self._account_row.add_prefix(self._account_icon)
        self._account_group.add(self._account_row)

        # One state-aware auth button — "Sign out" when signed in, "Sign in…"
        # when not. Never both at once; _set_auth_mode reconfigures the row
        # once we know the account state.
        self._logged_in = False
        self._auth_row = Adw.ActionRow()
        self._auth_btn = Gtk.Button()
        self._auth_btn.set_valign(Gtk.Align.CENTER)
        self._auth_btn.connect("clicked", self._on_auth_action)
        self._auth_row.add_suffix(self._auth_btn)
        self._auth_row.set_activatable_widget(self._auth_btn)
        self._account_group.add(self._auth_row)
        self._set_auth_mode(False)
        self._auth_btn.set_sensitive(False)  # enabled once status loads

        # Which claude binary Helios actually drives. Version skew between the
        # standalone install and IDE-bundled copies is silent otherwise (the
        # app once ran the VSCode extension's binary for weeks unnoticed).
        binary_row = Adw.ActionRow()
        binary_row.set_title("Claude binary")
        bin_icon = Gtk.Image.new_from_icon_name("application-x-executable-symbolic")
        bin_icon.add_css_class("dim-label")
        binary_row.add_prefix(bin_icon)
        try:
            from helios.backend.claude_binary import find_claude_binary

            binary = find_claude_binary()
            binary_row.set_subtitle(str(binary.path))
            source_badge = Gtk.Label(label=binary.source)
            source_badge.add_css_class("caption")
            source_badge.add_css_class("dim-label")
            source_badge.set_valign(Gtk.Align.CENTER)
            source_badge.set_tooltip_text(
                "Where this binary was found: env override, PATH, the "
                "standalone install, or an IDE bundle."
            )
            binary_row.add_suffix(source_badge)
        except Exception:
            binary_row.set_subtitle("Not found — set $HELIOS_CLAUDE_BINARY")
        binary_row.set_subtitle_selectable(True)
        self._account_group.add(binary_row)

        # --- Defaults ----------------------------------------------------
        defaults_group = Adw.PreferencesGroup()
        defaults_group.set_title("Defaults")
        defaults_group.set_description(
            "The model and safe permission mode are starting defaults. Use the "
            "Execution control beside the composer for the current conversation."
        )
        page.add(defaults_group)

        self._perm_row = Adw.ComboRow()
        self._perm_row.set_title("Default permissions")
        perm_model = Gtk.StringList()
        for _key, label, _desc in PERMISSION_MODES:
            perm_model.append(label)
        self._perm_row.set_model(perm_model)
        idx = next(
            (i for i, (k, _l, _d) in enumerate(PERMISSION_MODES) if k == permission_mode),
            0,
        )
        self._perm_row.set_selected(idx)
        self._perm_row.set_subtitle(self._permission_subtitle(idx))
        self._perm_row.connect("notify::selected", self._on_perm_changed)
        defaults_group.add(self._perm_row)

        self._model_row = Adw.ComboRow()
        self._model_row.set_title("Model")
        self._model_row.set_subtitle("Loading available models…")
        model_model = Gtk.StringList()
        for _key, label in self._model_choices:
            model_model.append(label)
        self._model_row.set_model(model_model)
        midx = next(
            (i for i, (k, _l) in enumerate(self._model_choices) if k == model), 0
        )
        self._model_row.set_selected(midx)
        self._model_row.connect("notify::selected", self._on_model_changed)
        defaults_group.add(self._model_row)

        self._cwd_row = Adw.ActionRow()
        self._cwd_row.set_title("Default working directory")
        self._cwd_row.set_subtitle_selectable(True)
        choose_cwd = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        choose_cwd.add_css_class("flat")
        choose_cwd.set_tooltip_text("Choose the working directory for new chats")
        choose_cwd.set_valign(Gtk.Align.CENTER)
        choose_cwd.connect("clicked", self._on_pick_default_cwd)
        self._cwd_row.add_suffix(choose_cwd)
        self._cwd_row.set_activatable_widget(choose_cwd)
        self._refresh_cwd_row()
        defaults_group.add(self._cwd_row)

        # --- Appearance (applied immediately, persisted in ui-state) ------
        from helios.backend import appearance

        appearance_ui = ui_state_store()
        appearance_group = Adw.PreferencesGroup()
        appearance_group.set_title("Appearance")
        page.add(appearance_group)

        theme_row = Adw.ComboRow()
        theme_row.set_title("Theme")
        theme_row.set_subtitle(
            "Automatic follows your system's dark-style setting; "
            "Light and Dark override it."
        )
        theme_model = Gtk.StringList()
        for _theme_label, _theme_value in appearance.SCHEME_CHOICES:
            theme_model.append(_theme_label)
        theme_row.set_model(theme_model)
        _saved_scheme = appearance.normalize_scheme(
            appearance_ui.get(appearance.UI_STATE_KEY, "auto")
        )
        theme_row.set_selected(
            next(
                (
                    i
                    for i, (_l, v) in enumerate(appearance.SCHEME_CHOICES)
                    if v == _saved_scheme
                ),
                0,
            )
        )

        def _on_theme_changed(row, _p) -> None:
            sel = row.get_selected()
            if 0 <= sel < len(appearance.SCHEME_CHOICES):
                value = appearance.SCHEME_CHOICES[sel][1]
                appearance_ui.set(appearance.UI_STATE_KEY, value)
                from helios.app import apply_color_scheme

                apply_color_scheme(value)

        theme_row.connect("notify::selected", _on_theme_changed)
        appearance_group.add(theme_row)

        glass_row = Adw.SwitchRow()
        glass_row.set_title("Translucent sessions sidebar")
        glass_row.set_subtitle(
            "Lets the desktop show through behind the session list. Needs a "
            "compositor blur: Blur My Shell → Applications, allowing "
            f"{APP_ID}, and Dynamic opacity OFF — it hides the blur on whichever "
            "window has focus, which is always this one while you are using it."
        )
        glass_row.set_active(
            appearance.normalize_glass(
                appearance_ui.get(appearance.GLASS_UI_STATE_KEY, False)
            )
        )

        def _on_glass_changed(row, _p) -> None:
            value = row.get_active()
            appearance_ui.set(appearance.GLASS_UI_STATE_KEY, value)
            # Apply to the live window rather than waiting for a restart. The
            # dialog is transient-for the main window, so that is the root.
            from helios.app import apply_glass

            window = self.get_root()
            transient = getattr(window, "get_transient_for", None)
            target = transient() if transient is not None else None
            apply_glass(target or window, value)

        glass_row.connect("notify::active", _on_glass_changed)
        appearance_group.add(glass_row)

        # --- Behavior (applied immediately, persisted in ui-state) -------
        behavior = Adw.PreferencesGroup()
        behavior.set_title("Behavior")
        page.add(behavior)
        ui = ui_state_store()

        notify_row = Adw.SwitchRow()
        notify_row.set_title("Notify when a background session finishes")
        notify_row.set_subtitle(
            "Desktop notification when a session you're not viewing completes a turn."
        )
        notify_row.set_active(bool(ui.get("notify_on_finish", True)))
        notify_row.connect(
            "notify::active",
            lambda r, _p: ui.set("notify_on_finish", r.get_active()),
        )
        behavior.add(notify_row)

        mission_gate_row = Adw.SwitchRow()
        mission_gate_row.set_title("Notify when a tandem mission is gated")
        mission_gate_row.set_subtitle(
            "Desktop notification when a mission run needs your approval to "
            "continue (the engine's own notification is best-effort/fire-once)."
        )
        mission_gate_row.set_active(bool(ui.get("notify_on_mission_gate", True)))
        mission_gate_row.connect(
            "notify::active",
            lambda r, _p: ui.set("notify_on_mission_gate", r.get_active()),
        )
        behavior.add(mission_gate_row)

        ollama_row = Adw.SwitchRow()
        ollama_row.set_title("Generate session titles with Ollama")
        ollama_row.set_subtitle(
            "Use Ollama on this computer or another server for short sidebar titles."
        )
        ollama_row.set_active(ui.get("title_backend", "ollama") == "ollama")
        ollama_row.connect(
            "notify::active",
            lambda r, _p: ui.set("title_backend", "ollama" if r.get_active() else "claude"),
        )
        behavior.add(ollama_row)
        self._ollama_url_row = Adw.EntryRow(title="Ollama server URL")
        self._ollama_url_row.set_text(str(ui.get("ollama_url", ollama_titles.DEFAULT_URL) or ollama_titles.DEFAULT_URL))
        self._ollama_model_row = Adw.EntryRow(title="Title model")
        self._ollama_model_row.set_text(str(ui.get("ollama_title_model", ollama_titles.DEFAULT_MODEL) or ollama_titles.DEFAULT_MODEL))
        behavior.add(self._ollama_url_row)
        behavior.add(self._ollama_model_row)
        self._ollama_status_row = Adw.ActionRow(title="Ollama connection")
        self._ollama_status_row.set_use_markup(False)
        self._ollama_status_row.set_subtitle("Save these settings and check model availability without generating text.")
        self._ollama_check_btn = Gtk.Button(label="Save and check")
        self._ollama_check_btn.set_valign(Gtk.Align.CENTER)
        self._ollama_check_btn.connect("clicked", self._on_ollama_check)
        self._ollama_status_row.add_suffix(self._ollama_check_btn)
        behavior.add(self._ollama_status_row)
        for row in (self._ollama_url_row, self._ollama_model_row):
            row.connect("changed", self._on_ollama_inputs_changed)

        pool_row = Adw.SwitchRow()
        pool_row.set_title("Show other machines' sessions")
        pool_row.set_subtitle(
            "List read-only sessions synced from other hosts in the shared "
            "iCloud pool (view-only — they can't be resumed here)."
        )
        pool_row.set_active(bool(ui.get("show_pool_sessions", False)))

        def _on_pool_toggle(r, _p) -> None:
            ui.set("show_pool_sessions", r.get_active())
            self.emit("pool-visibility-changed")

        pool_row.connect("notify::active", _on_pool_toggle)
        behavior.add(pool_row)
        self._pool_row = pool_row

        # --- Diagnostics: where to find logs for a bug report ---
        diag = Adw.PreferencesGroup()
        diag.set_title("Diagnostics")
        page.add(diag)
        log_path = log_file_path()
        log_row = Adw.ActionRow()
        log_row.set_title("Log file")
        log_row.set_subtitle(str(log_path))
        log_row.set_subtitle_selectable(True)
        open_log_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        open_log_btn.set_tooltip_text("Open log folder")
        open_log_btn.set_valign(Gtk.Align.CENTER)
        open_log_btn.add_css_class("flat")

        def _open_log_folder(_btn) -> None:
            uri = GLib.filename_to_uri(str(log_path.parent), None)
            launcher = Gtk.UriLauncher.new(uri)
            launcher.launch(self.get_root(), None, None)

        open_log_btn.connect("clicked", _open_log_folder)
        log_row.add_suffix(open_log_btn)
        diag.add(log_row)

        save_group = Adw.PreferencesGroup()
        page.add(save_group)
        save_btn = Gtk.Button(label="Save chat defaults")
        save_btn.add_css_class("suggested-action")
        save_btn.add_css_class("pill")
        save_btn.set_halign(Gtk.Align.END)
        save_btn.connect("clicked", self._on_save)
        save_group.add(save_btn)

    def _refresh_cwd_row(self) -> None:
        """Show the resolved directory, including HOME's read-only clamp.

        `$HOME` is where this lands with no setting saved, and it is silently
        clamped to read-only below the UI layer — which reads as "Helios won't
        edit anything" rather than "your workspace is wrong". Name it here.
        """

        resolved = default_cwd()
        if is_home_cwd(resolved):
            self._cwd_row.set_subtitle(
                f"{resolved} — HOME is read-only. New chats can inspect and "
                "answer; choose another directory to let them make changes."
            )
        else:
            self._cwd_row.set_subtitle(resolved)

    def _on_pick_default_cwd(self, _button) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose the default working directory for new chats")
        dialog.set_modal(True)

        def on_pick(dlg, result) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return  # user cancelled
            path = folder.get_path() if folder is not None else None
            if not path:
                return
            ui_state_store().set(DEFAULT_CWD_KEY, path)
            self._refresh_cwd_row()

        # Pin the dialog across the async call — see the composer's attach flow.
        self._cwd_dialog = dialog
        # An Adw.Dialog's root is the window it was presented in, but only once
        # presented; select_folder wants a Gtk.Window or None, so don't hand it
        # whatever else get_root() returns.
        root = self.get_root()
        dialog.select_folder(root if isinstance(root, Gtk.Window) else None, None, on_pick)

    def _on_perm_changed(self, *_args) -> None:
        idx = self._perm_row.get_selected()
        if 0 <= idx < len(PERMISSION_MODES):
            self._perm_row.set_subtitle(self._permission_subtitle(idx))

    @staticmethod
    def _permission_subtitle(idx: int) -> str:
        safe_idx = idx if 0 <= idx < len(PERMISSION_MODES) else 0
        key, _label, description = PERMISSION_MODES[safe_idx]
        prefix = "⚠ " if key == "bypassPermissions" else ""
        return f"{prefix}{_DEFAULT_PERMISSION_SCOPE} {description}"

    def _on_save(self, *_args) -> None:
        perm = PERMISSION_MODES[self._perm_row.get_selected()][0]
        self.emit("apply", perm, self._selected_model_id)

    def _on_model_changed(self, *_args) -> None:
        idx = self._model_row.get_selected()
        if 0 <= idx < len(self._model_choices):
            self._selected_model_id = self._model_choices[idx][0]

    # ── page 2: providers ───────────────────────────────────────────────

    def _build_providers_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Providers")
        page.set_icon_name("network-server-symbolic")
        self.add(page)

        # ── OpenRouter · Chat provider ──
        self._or_group = Adw.PreferencesGroup()
        self._or_group.set_title("OpenRouter · Chat")
        self._or_group.set_description(
            "OpenRouter as a direct chat provider with tool use. "
            "Enter an API key (sk-or-…) to enable the OpenRouter toggle."
        )
        page.add(self._or_group)

        self._or_key_row = Adw.PasswordEntryRow()
        self._or_key_row.set_title("OpenRouter API key")
        self._or_key_row.set_show_apply_button(True)
        self._or_key_row.connect("apply", self._on_or_key_apply)
        self._or_group.add(self._or_key_row)

        self._or_status_row = Adw.ActionRow()
        self._or_status_row.set_title("Key not set")
        self._or_status_row.set_subtitle("Add an API key to enable OpenRouter chat.")
        self._or_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self._or_icon.add_css_class("dim-label")
        self._or_status_row.add_prefix(self._or_icon)
        self._or_group.add(self._or_status_row)

        self._or_catalog_row = Adw.ActionRow()
        self._or_catalog_row.set_title("Model catalog")
        self._or_catalog_row.set_subtitle("Not loaded yet.")
        cat_icon = Gtk.Image.new_from_icon_name("view-list-symbolic")
        cat_icon.add_css_class("dim-label")
        self._or_catalog_row.add_prefix(cat_icon)
        self._or_group.add(self._or_catalog_row)

        or_refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        or_refresh.add_css_class("flat")
        or_refresh.set_tooltip_text("Re-check OpenRouter key and refresh models")
        or_refresh.set_valign(Gtk.Align.CENTER)
        or_refresh.connect("clicked", lambda *_: self._reload_openrouter())
        self._or_group.set_header_suffix(or_refresh)

        self._build_or_picker_group(page)

        # ── OpenRouter · Smart Routing (broker) ──
        self._router_group = Adw.PreferencesGroup()
        self._router_group.set_title("OpenRouter · Smart Routing")
        self._router_group.set_description(
            "Helios keeps Claude or GPT as the primary driver and delegates "
            "only bounded work through versioned, privacy-pinned profiles. "
            "Quality gates come first, cost second, and speed third."
        )
        self._router_refresh = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic"
        )
        self._router_refresh.add_css_class("flat")
        self._router_refresh.set_tooltip_text("Re-check Helios Router")
        self._router_refresh.set_valign(Gtk.Align.CENTER)
        self._router_refresh.connect("clicked", lambda *_: self._reload_router())
        self._router_group.set_header_suffix(self._router_refresh)
        page.add(self._router_group)

        self._router_status_row = Adw.ActionRow()
        self._router_status_row.set_title("Checking Helios Router…")
        self._router_status_row.set_subtitle(
            "Credential health, profile eligibility, and receipts load locally."
        )
        self._router_icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
        self._router_icon.add_css_class("dim-label")
        self._router_status_row.add_prefix(self._router_icon)
        self._router_group.add(self._router_status_row)

        self._router_switch = Adw.SwitchRow()
        self._router_switch.set_title("Enable Smart Routing preview")
        self._router_switch.set_subtitle(
            "Evaluates route candidates, but sends no task upstream until "
            "trusted context, endpoint, and paired-quality gates are approved."
        )
        self._router_switch.set_active(False)
        self._router_switch.set_sensitive(False)
        self._router_switch.connect("notify::active", self._on_router_toggle)
        self._router_group.add(self._router_switch)

        self._router_profiles_row = Adw.ActionRow()
        self._router_profiles_row.set_title("Quality-gated profile catalog")
        self._router_profiles_row.set_subtitle(
            "Loading canary and quarantine states…"
        )
        profile_icon = Gtk.Image.new_from_icon_name("view-list-symbolic")
        profile_icon.add_css_class("dim-label")
        self._router_profiles_row.add_prefix(profile_icon)
        self._router_group.add(self._router_profiles_row)

        self._codex_group = Adw.PreferencesGroup()
        self._codex_group.set_title("OpenAI · Codex")
        self._codex_group.set_description(
            "Helios asks Codex App Server for the models and reasoning modes "
            "your current ChatGPT or API-key account can actually use. New "
            "model families appear from that live capability catalog."
        )
        page.add(self._codex_group)

        refresh_codex = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_codex.add_css_class("flat")
        refresh_codex.set_tooltip_text("Re-check Codex status and refresh the model list")
        refresh_codex.set_valign(Gtk.Align.CENTER)
        refresh_codex.connect("clicked", lambda *_: self._reload_codex(force_models=True))
        self._codex_group.set_header_suffix(refresh_codex)

        self._codex_status_row = Adw.ActionRow()
        self._codex_status_row.set_title("Checking…")
        self._codex_icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
        self._codex_icon.add_css_class("dim-label")
        self._codex_status_row.add_prefix(self._codex_icon)
        self._codex_group.add(self._codex_status_row)

        self._codex_catalog_row = Adw.ActionRow()
        self._codex_catalog_row.set_title("Model catalog")
        self._codex_catalog_row.set_subtitle("Loading Codex capabilities…")
        catalog_icon = Gtk.Image.new_from_icon_name("view-list-symbolic")
        catalog_icon.add_css_class("dim-label")
        self._codex_catalog_row.add_prefix(catalog_icon)
        self._codex_group.add(self._codex_catalog_row)

        self._key_row = Adw.PasswordEntryRow()
        self._key_row.set_title("Use or replace an OpenAI API key")
        self._key_row.set_show_apply_button(True)
        self._key_row.connect("apply", self._on_key_apply)
        self._codex_group.add(self._key_row)

        self._key_hint = Adw.ActionRow()
        self._key_hint.set_title("How this is stored")
        self._key_hint.set_subtitle(
            "The key goes to `codex login` and is stored by Codex (file, "
            "keyring, or your configured credential backend) — Helios keeps "
            "no copy. Existing ChatGPT-account sign-in works directly; an API "
            "key is optional."
        )
        self._key_hint.add_css_class("dim-label")
        self._codex_group.add(self._key_hint)

    def _reload_codex(self, *, force_models: bool = False) -> None:
        if self._codex_update_running:
            return
        self._codex_generation += 1
        generation = self._codex_generation
        self._codex_status_row.set_title("Checking…")
        self._codex_status_row.set_subtitle("")
        self._set_codex_icon("content-loading-symbolic", "dim-label")

        def work() -> tuple:
            auth = codex_env.fetch_auth_status()
            version = codex_env.codex_version()
            try:
                anthropic = model_catalog.anthropic_entries()
            except Exception:  # noqa: BLE001 — Settings must always retain a picker
                anthropic = list(model_catalog.FALLBACK_ANTHROPIC)
            models, status = model_catalog.openai_entries(
                force=force_models,
                auth=auth,
            )
            return auth, version, anthropic, models, status

        self._run_async(
            work,
            lambda result: self._apply_codex(result, generation=generation),
        )

    def _apply_codex(self, result, *, generation: int | None = None) -> None:
        if (
            self._closed
            or isinstance(result, Exception)
            or self._codex_update_running
            or (generation is not None and generation != self._codex_generation)
        ):
            return
        auth, version, anthropic, models, status = result
        selectable_models = SettingsDialog._selectable_openai_models(models, status)
        self._set_model_choices(self._entry_pairs(anthropic + selectable_models))
        if not auth.ok and not auth.logged_in:
            self._codex_status_row.set_title("Codex CLI not available")
            self._codex_status_row.set_subtitle(auth.detail)
            self._set_codex_icon("dialog-error-symbolic", "error")
            self._codex_catalog_row.set_subtitle("Unavailable until Codex starts")
            return
        if not auth.logged_in:
            self._codex_status_row.set_title("Not signed in")
            self._codex_status_row.set_subtitle(
                "Paste an API key below to enable OpenAI models."
            )
            self._set_codex_icon("dialog-warning-symbolic", "warning")
            self._codex_catalog_row.set_subtitle("Sign in to discover models")
            return
        bits = [version or "codex"]
        if selectable_models:
            bits.append(f"{len(selectable_models)} models available")
        elif status == "chatgpt-fallback":
            bits.append("informational catalog fallback; unverified")
        elif status != "app-server":
            bits.append("model list unavailable")
        self._codex_status_row.set_title(auth.detail)
        self._codex_status_row.set_subtitle(" · ".join(bits))
        self._set_codex_icon("object-select-symbolic", "success")

        source_label = {
            "app-server": "Live from Codex App Server",
            "app-server-empty": "Codex App Server returned no usable models",
            "chatgpt-fallback": (
                "Informational fallback · unverified for activation · not selectable"
            ),
            "access_token-catalog-unavailable": "Live account; catalog unavailable",
            "authenticated-catalog-unavailable": "Live account; catalog unavailable",
            "apikey-catalog-unavailable": "Live account; catalog unavailable",
        }.get(status, status or "Unavailable")
        preferred = model_catalog.preferred_openai_model(selectable_models)
        default_entry = next(
            (entry for entry in selectable_models if entry.id == preferred),
            None,
        )
        detail = source_label
        if default_entry is not None:
            detail += f" · Default: {default_entry.label}"
            efforts = [key for key, _desc in default_entry.reasoning_efforts]
            if efforts:
                detail += f" · Reasoning: {' / '.join(efforts)}"
            if default_entry.service_tiers:
                detail += " · Fast tier available"
        self._codex_catalog_row.set_subtitle(detail)

    def _set_codex_icon(self, icon_name: str, css: str) -> None:
        self._codex_icon.set_from_icon_name(icon_name)
        for c in ("success", "warning", "error", "dim-label"):
            self._codex_icon.remove_css_class(c)
        self._codex_icon.add_css_class(css)

    def _on_key_apply(self, row: Adw.PasswordEntryRow) -> None:
        key = (row.get_text() or "").strip()
        if not key:
            return
        self._codex_status_row.set_title("Saving key…")
        self._set_codex_icon("content-loading-symbolic", "dim-label")
        row.set_sensitive(False)
        self._codex_update_running = True
        # Invalidate constructor/manual loaders that belong to the old account.
        self._codex_generation += 1

        def runner() -> None:
            result = self._save_codex_api_key(key)
            # Completion is durable: this idle always runs even if the dialog
            # closes, so MainWindow learns that its catalog identity changed.
            GLib.idle_add(self._finish_key_update, result)

        threading.Thread(target=runner, daemon=True).start()

    def _finish_key_update(self, result) -> bool:
        self._codex_update_running = False
        # Invalidate any refresh that was queued during credential rotation.
        self._codex_generation += 1
        mutation_attempted = (
            isinstance(result, tuple)
            and len(result) >= 4
            and result[3] != "active-gpt-sessions"
        )
        if mutation_attempted and self._codex_credentials_changed is not None:
            try:
                self._codex_credentials_changed()
            except Exception:  # noqa: BLE001 — app callback cannot break GTK idle
                pass
        if not self._closed:
            self._apply_key_result(result)
        return False

    # ── OpenRouter key + catalog ─────────────────────────────────────────

    def _on_or_key_apply(self, row: Adw.PasswordEntryRow) -> None:
        key = (row.get_text() or "").strip()
        if not key:
            return
        row.set_sensitive(False)
        self._or_status_row.set_title("Saving…")

        def runner() -> None:
            from helios.backend.openrouter import key as or_key
            try:
                or_key.save_key(key)
                ok = True
            except or_key.KeyValidationError:
                ok = False
            GLib.idle_add(self._finish_or_key_save, ok)

        threading.Thread(target=runner, daemon=True).start()

    def _finish_or_key_save(self, ok: bool) -> bool:
        self._or_key_row.set_sensitive(True)
        self._or_key_row.set_text("")
        if not ok:
            self._or_status_row.set_title("Invalid key")
            self._or_status_row.set_subtitle("The key was too short or malformed.")
            return False
        self._reload_openrouter()
        if self._openrouter_credentials_changed is not None:
            try:
                self._openrouter_credentials_changed()
            except Exception:  # noqa: BLE001
                pass
        return False

    # ── OpenRouter picker shortlist ──────────────────────────────────────

    def _build_or_picker_group(self, page: Adw.PreferencesPage) -> None:
        """Choose which OpenRouter models the chat picker offers up front.

        OpenRouter routes several hundred models. Nobody picks from that list;
        they pick from the five they use. Ticked models become the picker's
        first tier — the rest stay reachable behind "Older models" and search,
        so this narrows the default view without hiding the catalog.
        """
        group = Adw.PreferencesGroup()
        group.set_title("OpenRouter · Models in the picker")
        group.set_description(
            "Tick the models you actually use. Untick everything to show the "
            "whole catalog."
        )
        page.add(group)
        self._or_picker_group = group

        self._or_picker_count = Adw.ActionRow()
        self._or_picker_count.set_title("No models selected")
        self._or_picker_count.set_subtitle("The picker shows the full catalog.")
        clear = Gtk.Button(label="Clear")
        clear.add_css_class("flat")
        clear.set_valign(Gtk.Align.CENTER)
        clear.connect("clicked", lambda *_: self._set_or_picked(set()))
        self._or_picker_count.add_suffix(clear)
        group.add(self._or_picker_count)

        search = Gtk.SearchEntry()
        search.set_placeholder_text("Filter models…")
        search.connect("search-changed", lambda e: self._fill_or_picker(e.get_text()))
        search_row = Adw.PreferencesRow()
        search_row.set_activatable(False)
        search.set_margin_top(6)
        search.set_margin_bottom(6)
        search.set_margin_start(6)
        search.set_margin_end(6)
        search_row.set_child(search)
        group.add(search_row)

        self._or_picker_list = Gtk.ListBox()
        self._or_picker_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._or_picker_list.add_css_class("boxed-list")
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(220)
        scroller.set_max_content_height(320)
        scroller.set_child(self._or_picker_list)
        list_row = Adw.PreferencesRow()
        list_row.set_activatable(False)
        list_row.set_child(scroller)
        group.add(list_row)

        self._or_picker_entries: list = []
        self._fill_or_picker("")
        self._refresh_or_picker_count()

    def _or_picked(self) -> set[str]:
        stored = ui_state_store().get(OPENROUTER_PICKER_KEY, []) or []
        return {str(x) for x in stored if isinstance(x, str)}

    def _set_or_picked(self, ids: set[str]) -> None:
        ui_state_store().set(OPENROUTER_PICKER_KEY, sorted(ids))
        self._refresh_or_picker_count()

    def _refresh_or_picker_count(self) -> None:
        picked = self._or_picked()
        if picked:
            self._or_picker_count.set_title(
                f"{len(picked)} model{'s' if len(picked) != 1 else ''} selected"
            )
            self._or_picker_count.set_subtitle(
                "These appear first in the chat model picker."
            )
        else:
            self._or_picker_count.set_title("No models selected")
            self._or_picker_count.set_subtitle("The picker shows the full catalog.")

    def _fill_or_picker(self, query: str) -> None:
        if not self._or_picker_entries:
            from helios.backend.openrouter import catalog as or_catalog

            self._or_picker_entries = or_catalog.cached_models()

        while (row := self._or_picker_list.get_first_child()) is not None:
            self._or_picker_list.remove(row)

        picked = self._or_picked()
        needle = (query or "").strip().lower()
        matches = [
            e for e in self._or_picker_entries
            if not needle or needle in e.id.lower() or needle in e.label.lower()
        ]
        if not matches:
            empty = Adw.ActionRow()
            empty.set_title(
                "No models match" if needle else "Catalog not loaded yet"
            )
            empty.set_subtitle(
                "" if needle else "Add a key, then use the refresh button above."
            )
            self._or_picker_list.append(empty)
            return
        # Selected first: the list is long, and what you already chose is what
        # you want to review.
        matches.sort(key=lambda e: (e.id not in picked, e.group, e.label.lower()))
        for entry in matches[:_OR_PICKER_ROW_CAP]:
            self._or_picker_list.append(self._or_picker_row(entry, picked))
        if len(matches) > _OR_PICKER_ROW_CAP:
            more = Adw.ActionRow()
            more.set_title(f"…and {len(matches) - _OR_PICKER_ROW_CAP} more")
            more.set_subtitle("Narrow the filter to reach them.")
            self._or_picker_list.append(more)

    def _or_picker_row(self, entry, picked: set[str]) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(entry.label)
        row.set_subtitle(f"{entry.id} · {entry.description}" if entry.description else entry.id)
        check = Gtk.CheckButton()
        check.set_active(entry.id in picked)
        check.set_valign(Gtk.Align.CENTER)
        check.connect("toggled", self._on_or_pick_toggled, entry.id)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        return row

    def _on_or_pick_toggled(self, check: Gtk.CheckButton, model_id: str) -> None:
        picked = self._or_picked()
        if check.get_active():
            picked.add(model_id)
        else:
            picked.discard(model_id)
        self._set_or_picked(picked)

    def _reload_openrouter(self) -> None:
        from helios.backend.openrouter import key as or_key

        has_key = bool(or_key.load_key())
        if has_key:
            self._or_status_row.set_title("Key saved")
            self._or_status_row.set_subtitle("OpenRouter chat is enabled.")
            self._or_icon.set_from_icon_name("object-select-symbolic")
        else:
            self._or_status_row.set_title("Key not set")
            self._or_status_row.set_subtitle("Add an API key to enable OpenRouter chat.")
            self._or_icon.set_from_icon_name("dialog-warning-symbolic")
        for c in ("success", "warning", "error", "dim-label"):
            self._or_icon.remove_css_class(c)
        self._or_icon.add_css_class("dim-label")
        self._or_catalog_row.set_subtitle("Loading…" if has_key else "Not loaded.")
        if not has_key:
            return

        def runner() -> None:
            from helios.backend import model_catalog as mc
            entries, status = mc.openrouter_entries(force=True)
            GLib.idle_add(self._apply_or_catalog, entries, status)

        threading.Thread(target=runner, daemon=True).start()

    def _apply_or_catalog(self, entries: list, status: str) -> bool:
        if self._closed:
            return False
        if entries:
            self._or_catalog_row.set_subtitle(
                f"{len(entries)} models available ({status})."
            )
        else:
            self._or_catalog_row.set_subtitle(
                f"No models loaded ({status}). Check your connection."
            )
        if entries:
            # The shortlist picker reads the same catalog — refill it from the
            # rows that just landed rather than the stale cache it started on.
            self._or_picker_entries = list(entries)
            self._fill_or_picker("")
        return False

    @staticmethod
    def _save_codex_api_key(key: str) -> tuple[bool, str, list, str]:
        """Change the Codex credential and discover its catalog atomically.

        The shared hub lease prevents a persistent App Server from retaining
        the previous account and prevents a new session from starting midway
        through the update.  Exact credential text is redacted from every
        user-visible failure returned by this boundary.
        """
        key = (key or "").strip()
        try:
            with get_shared_hub().credential_update():
                ok, _detail = codex_env.login_with_api_key(key)
                if not ok:
                    # Codex stderr is untrusted at this credential boundary: a
                    # CLI could echo a full or partial key. Never reflect it.
                    return (
                        False,
                        "Codex rejected the credential. Check the key and try again.",
                        [],
                        "credential-update-failed",
                    )
                try:
                    models, status = model_catalog.openai_entries(force=True)
                except Exception:  # noqa: BLE001 — login already mutated credentials
                    # The durable app callback must still run after a successful
                    # login even if capability discovery fails unexpectedly.
                    models, status = [], "credential-update-catalog-unavailable"
                return (
                    True,
                    "Key saved by Codex's configured credential store.",
                    models,
                    status,
                )
        except CodexCredentialUpdateInUseError as exc:
            return False, str(exc), [], "active-gpt-sessions"
        except Exception:  # noqa: BLE001 — never surface a credential-bearing error
            return False, "Codex credential update failed.", [], "credential-update-error"

    def _apply_key_result(self, result) -> None:
        if self._closed:
            return
        self._key_row.set_sensitive(True)
        if isinstance(result, Exception):
            self._replace_openai_choices([])
            self._codex_status_row.set_title("Key save failed")
            self._codex_status_row.set_subtitle("Codex credential update failed.")
            self._set_codex_icon("dialog-error-symbolic", "error")
            return
        ok, msg, models, status = result
        if not ok:
            blocked = status == "active-gpt-sessions"
            if not blocked:
                # A non-zero CLI exit can still follow a partial credential
                # store mutation. Fail closed in this dialog while the durable
                # app callback re-checks the authoritative account.
                self._replace_openai_choices([])
            self._codex_status_row.set_title(
                "Close active GPT sessions first" if blocked else "Key save failed"
            )
            self._codex_status_row.set_subtitle(msg)
            self._set_codex_icon(
                "dialog-warning-symbolic" if blocked else "dialog-error-symbolic",
                "warning" if blocked else "error",
            )
            return
        self._key_row.set_text("")
        selectable_models = SettingsDialog._selectable_openai_models(models, status)
        self._replace_openai_choices(selectable_models)
        if selectable_models:
            self._codex_status_row.set_title("Signed in")
            self._codex_status_row.set_subtitle(
                f"{msg} {len(selectable_models)} models available."
            )
            self._set_codex_icon("object-select-symbolic", "success")
        elif status == "chatgpt-fallback":
            self._codex_status_row.set_title("Key saved; GPT catalog unverified")
            self._codex_status_row.set_subtitle(
                f"{msg} Curated fallback is informational only; "
                "no GPT models were activated."
            )
            self._set_codex_icon("dialog-warning-symbolic", "warning")
        else:
            self._codex_status_row.set_title("Key saved, but model catalog unavailable")
            self._codex_status_row.set_subtitle(
                f"{status} — Codex could not verify this account's models."
            )
            self._set_codex_icon("dialog-warning-symbolic", "warning")

    def _replace_openai_choices(self, models: list[model_catalog.ModelEntry]) -> None:
        """Replace prior GPT rows with caller-verified App Server entries.

        Callers must pass an empty list for every status except ``app-server``.
        An empty authoritative result clears old-account choices decisively.
        """
        non_openai = [
            pair for pair in self._model_choices
            if model_catalog.provider_for(pair[0]) != model_catalog.PROVIDER_OPENAI
        ]
        available_openai = {entry.id for entry in models}
        if (
            model_catalog.provider_for(self._selected_model_id)
            == model_catalog.PROVIDER_OPENAI
            and self._selected_model_id not in available_openai
        ):
            self._selected_model_id = (
                model_catalog.preferred_openai_model(models)
                or (non_openai[0][0] if non_openai else "")
            )
        # Replace, never merge, the previous account's GPT rows. An empty
        # authoritative result must clear them just as decisively as a new list.
        self._set_model_choices(non_openai + self._entry_pairs(models))

    # ── page 3: tools ────────────────────────────────────────────────────

    def _build_tools_page(self) -> None:
        page = Adw.PreferencesPage()
        page.set_title("Tools")
        page.set_icon_name("applications-utilities-symbolic")
        self.add(page)

        self._router_tool_group = Adw.PreferencesGroup()
        self._router_tool_group.set_title("Helios Router")
        self._router_tool_group.set_description(
            "The same provider-neutral schemas reach Claude through MCP and "
            "GPT through thread-bound native dynamic tools. Neither client "
            "receives the OpenRouter credential."
        )
        page.add(self._router_tool_group)
        self._router_tool_row = Adw.ActionRow()
        self._router_tool_row.set_title("Checking broker…")
        self._router_tool_row.set_subtitle(
            "delegate_task · explain_route · routing_status · specialty discovery"
        )
        self._router_tool_icon = Gtk.Image.new_from_icon_name(
            "content-loading-symbolic"
        )
        self._router_tool_icon.add_css_class("dim-label")
        self._router_tool_row.add_prefix(self._router_tool_icon)
        self._router_tool_group.add(self._router_tool_row)

        # --- MCP servers -------------------------------------------------
        self._mcp_group = Adw.PreferencesGroup()
        self._mcp_group.set_title("Claude MCP servers")
        self._mcp_group.set_description(
            "External tool servers available to Claude (via `claude mcp list`)."
        )
        page.add(self._mcp_group)

        refresh_mcp = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_mcp.add_css_class("flat")
        refresh_mcp.set_tooltip_text("Re-check MCP servers")
        refresh_mcp.set_valign(Gtk.Align.CENTER)
        refresh_mcp.connect("clicked", lambda *_: self._reload_mcp())
        self._mcp_group.set_header_suffix(refresh_mcp)

        self._mcp_rows: list[Gtk.Widget] = []

        self._codex_mcp_group = Adw.PreferencesGroup()
        self._codex_mcp_group.set_title("Codex MCP servers")
        self._codex_mcp_group.set_description(
            "Live inventory from Helios's shared Codex App Server connection."
        )
        page.add(self._codex_mcp_group)
        refresh_codex_mcp = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_codex_mcp.add_css_class("flat")
        refresh_codex_mcp.set_tooltip_text("Reload the latest Codex MCP inventory")
        refresh_codex_mcp.set_valign(Gtk.Align.CENTER)
        refresh_codex_mcp.connect("clicked", lambda *_: self._reload_codex_mcp())
        self._codex_mcp_group.set_header_suffix(refresh_codex_mcp)
        self._codex_mcp_rows: list[Gtk.Widget] = []

        # --- Built-in tools ----------------------------------------------
        self._builtin_group = Adw.PreferencesGroup()
        self._builtin_group.set_title("Built-in tools")
        page.add(self._builtin_group)
        self._reload_builtin()

    # ── Helios Router ---------------------------------------------------

    def _reload_router(self) -> None:
        if self._router_mutating:
            return
        self._router_generation += 1
        generation = self._router_generation
        self._router_status_row.set_title("Checking Helios Router…")
        self._set_router_icon("content-loading-symbolic", "dim-label")
        self._router_switch.set_sensitive(False)
        self._router_refresh.set_sensitive(False)
        self._run_async(
            lambda: RouterClient().status(),
            lambda result: self._apply_router_status(result, generation),
        )

    def _apply_router_status(
        self,
        result: object,
        generation: int,
    ) -> None:
        if self._closed or generation != self._router_generation:
            return
        self._router_mutating = False
        self._router_refresh.set_sensitive(True)
        if isinstance(result, Exception) or not isinstance(result, dict):
            self._router_status_row.set_title("Helios Router unavailable")
            self._router_status_row.set_subtitle(
                _router_unavailable_subtitle(self._router_last_enabled)
            )
            self._router_profiles_row.set_subtitle("No profiles are dispatchable.")
            self._router_tool_row.set_title("Broker unavailable")
            self._set_router_icon("dialog-error-symbolic", "error")
            self._set_router_tool_icon("dialog-error-symbolic", "error")
            self._router_syncing = True
            try:
                self._router_switch.set_active(
                    bool(self._router_last_enabled)
                )
            finally:
                self._router_syncing = False
            self._router_switch.set_sensitive(False)
            return

        enabled = bool(result.get("enabled", False))
        self._router_last_enabled = enabled
        automatic_dispatch = bool(result.get("automatic_dispatch", False))
        credential = str(result.get("credential_status") or "unknown")
        credential_ready = credential == "healthy"
        profiles = result.get("profiles")
        profiles = [row for row in profiles if isinstance(row, dict)] if isinstance(
            profiles, list
        ) else []
        eligible = [
            row for row in profiles
            if str(row.get("stage") or "") in {"canary", "role_approved"}
            and bool(row.get("eligible", False))
        ]
        awaiting = _router_awaiting_count(profiles)
        mode = "preview on" if enabled else "off"
        state = "Ready" if credential_ready else "Degraded"
        self._router_status_row.set_title(f"{state} · Smart Routing {mode}")
        self._router_status_row.set_subtitle(
            f"Credential {credential} · {len(eligible)} eligible · "
            f"{awaiting} awaiting evaluation"
        )
        self._router_profiles_row.set_subtitle(
            _router_profile_summary(profiles)
        )
        self._router_tool_row.set_title(
            "Broker connected · delegation dispatchable"
            if automatic_dispatch
            else "Broker connected · evaluation hold"
        )
        if credential_ready:
            self._set_router_icon("object-select-symbolic", "success")
            self._set_router_tool_icon(
                "object-select-symbolic"
                if automatic_dispatch
                else "dialog-warning-symbolic",
                "success" if automatic_dispatch else "warning",
            )
        else:
            self._set_router_icon("dialog-warning-symbolic", "warning")
            self._set_router_tool_icon("dialog-warning-symbolic", "warning")
        self._router_syncing = True
        try:
            self._router_switch.set_active(enabled)
        finally:
            self._router_syncing = False
        # An unhealthy credential can always be turned off, never on. The
        # refresh button re-checks health without reopening Settings.
        self._router_switch.set_sensitive(credential_ready or enabled)

    def _on_router_toggle(self, row: Adw.SwitchRow, _param) -> None:
        if self._router_syncing or not row.get_sensitive():
            return
        desired = bool(row.get_active())
        self._router_mutating = True
        self._router_generation += 1
        mutation_generation = self._router_generation
        row.set_sensitive(False)
        self._router_refresh.set_sensitive(False)
        self._router_status_row.set_title("Applying Smart Routing policy…")

        def done(_result: object) -> None:
            if (
                self._closed
                or mutation_generation != self._router_generation
            ):
                return
            # A timeout is an ambiguous mutation, not proof of rollback. Always
            # reconcile from a new authoritative status read.
            self._router_status_row.set_title("Confirming Smart Routing policy…")
            self._router_generation += 1
            reconcile_generation = self._router_generation
            self._run_async(
                lambda: RouterClient().status(),
                lambda result: self._apply_router_status(
                    result,
                    reconcile_generation,
                ),
            )

        self._run_async(lambda: RouterClient().set_enabled(desired), done)

    def _set_router_icon(self, icon_name: str, css: str) -> None:
        self._router_icon.set_from_icon_name(icon_name)
        for name in ("success", "warning", "error", "dim-label"):
            self._router_icon.remove_css_class(name)
        self._router_icon.add_css_class(css)

    def _set_router_tool_icon(self, icon_name: str, css: str) -> None:
        self._router_tool_icon.set_from_icon_name(icon_name)
        for name in ("success", "warning", "error", "dim-label"):
            self._router_tool_icon.remove_css_class(name)
        self._router_tool_icon.add_css_class(css)

    def _reload_builtin(self) -> None:
        names, source = claude_env.builtin_tools()
        if source == "session":
            self._builtin_group.set_description(
                f"{len(names)} tools active in your last session."
            )
        else:
            self._builtin_group.set_description(
                "Standard Claude Code tools — start a chat to capture the exact "
                "set for your config."
            )
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_max_children_per_line(30)
        flow.set_column_spacing(6)
        flow.set_row_spacing(6)
        flow.set_margin_top(8)
        flow.set_margin_bottom(4)
        flow.set_homogeneous(False)
        for name in names:
            chip = Gtk.Label(label=name)
            chip.add_css_class("helios-tool-chip")
            flow.append(chip)
        self._builtin_group.add(flow)

    # ── live loads (threaded) ────────────────────────────────────────────

    def _reload_account(self) -> None:
        self._account_row.set_title("Checking…")
        self._account_row.set_subtitle("")
        self._set_account_icon("content-loading-symbolic", "dim-label")
        self._auth_btn.set_sensitive(False)
        self._run_async(claude_env.fetch_auth_status, self._apply_account)

    def _apply_account(self, status: claude_env.AuthStatus) -> None:
        if self._closed:
            return
        if not status.ok:
            self._account_row.set_title("Couldn't read account status")
            self._account_row.set_subtitle(status.error)
            self._set_account_icon("dialog-error-symbolic", "error")
            self._set_auth_mode(False)
            return
        if not status.logged_in:
            self._account_row.set_title("Not signed in")
            self._account_row.set_subtitle("Use “Sign in” below to authenticate.")
            self._set_account_icon("dialog-warning-symbolic", "warning")
            self._set_auth_mode(False)
            return
        self._account_row.set_title(status.email or "Signed in")
        bits = []
        if status.subscription_type:
            bits.append(status.subscription_type.capitalize())
        if status.auth_method:
            bits.append(status.auth_method)
        # Show a real (team) org name, but skip the auto-generated personal
        # "<email>'s Organization" — it's just noise next to the email title.
        if (
            status.org_name
            and status.org_name != status.email
            and status.org_name != f"{status.email}'s Organization"
        ):
            bits.append(status.org_name)
        self._account_row.set_subtitle(" · ".join(bits) or "Signed in")
        self._set_account_icon("object-select-symbolic", "success")
        self._set_auth_mode(True)

    def _set_account_icon(self, icon_name: str, css: str) -> None:
        self._account_icon.set_from_icon_name(icon_name)
        for c in ("success", "warning", "error", "dim-label"):
            self._account_icon.remove_css_class(c)
        self._account_icon.add_css_class(css)

    def _reload_mcp(self) -> None:
        self._clear_mcp_rows()
        spinner_row = Adw.ActionRow()
        spinner_row.set_title("Checking MCP servers…")
        spin = Gtk.Spinner()
        spin.start()
        spinner_row.add_prefix(spin)
        self._mcp_group.add(spinner_row)
        self._mcp_rows.append(spinner_row)
        self._run_async(claude_env.fetch_mcp_servers, self._apply_mcp)

    def _apply_mcp(self, result: tuple[list, str]) -> None:
        if self._closed:
            return
        servers, error = result
        self._clear_mcp_rows()
        if error:
            row = Adw.ActionRow()
            row.set_title("Couldn't list MCP servers")
            row.set_subtitle(error)
            icon = Gtk.Image.new_from_icon_name("dialog-error-symbolic")
            icon.add_css_class("error")
            row.add_prefix(icon)
            self._mcp_group.add(row)
            self._mcp_rows.append(row)
            return
        if not servers:
            row = Adw.ActionRow()
            row.set_title("No MCP servers configured")
            row.set_subtitle("Add one with `claude mcp add …`.")
            self._mcp_group.add(row)
            self._mcp_rows.append(row)
            return
        for srv in servers:
            self._mcp_group.add(self._make_mcp_row(srv))

    def _make_mcp_row(self, srv: claude_env.McpServer) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(srv.name)
        row.set_subtitle(srv.target)
        row.set_subtitle_lines(1)

        transport_icon = Gtk.Image.new_from_icon_name(
            "network-server-symbolic" if srv.transport == "http"
            else "utilities-terminal-symbolic"
        )
        transport_icon.add_css_class("dim-label")
        transport_icon.set_tooltip_text(srv.transport)
        row.add_prefix(transport_icon)

        icon_name, css = _HEALTH_ICON.get(srv.health, _HEALTH_ICON["unknown"])
        suffix = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if srv.status_text:
            lbl = Gtk.Label(label=srv.status_text)
            lbl.add_css_class("caption")
            lbl.add_css_class("dim-label")
            suffix.append(lbl)
        health_icon = Gtk.Image.new_from_icon_name(icon_name)
        health_icon.add_css_class(css)
        suffix.append(health_icon)
        suffix.set_valign(Gtk.Align.CENTER)
        row.add_suffix(suffix)
        self._mcp_rows.append(row)
        return row

    def _clear_mcp_rows(self) -> None:
        for row in self._mcp_rows:
            self._mcp_group.remove(row)
        self._mcp_rows = []

    def _reload_codex_mcp(self) -> None:
        self.set_codex_mcp_servers(codex_env.load_mcp_snapshot())

    def set_codex_mcp_servers(self, servers) -> None:
        """Render a live or cached native Codex MCP inventory."""

        if self._closed:
            return
        for row in self._codex_mcp_rows:
            self._codex_mcp_group.remove(row)
        self._codex_mcp_rows = []
        servers = (
            [server for server in servers if isinstance(server, dict)]
            if isinstance(servers, list)
            else []
        )
        if not servers:
            row = Adw.ActionRow()
            row.set_title("No live Codex MCP snapshot yet")
            row.set_subtitle("Start a GPT chat to load its native tool inventory.")
            self._codex_mcp_group.add(row)
            self._codex_mcp_rows.append(row)
            return
        for server in servers:
            row = self._make_codex_mcp_row(server)
            self._codex_mcp_group.add(row)
            self._codex_mcp_rows.append(row)

    def _make_codex_mcp_row(self, server: dict) -> Adw.ActionRow:
        row = Adw.ActionRow()
        row.set_title(str(server.get("name") or "MCP server"))
        tools = server.get("tools")
        tool_count = len(tools) if isinstance(tools, dict) else 0
        auth = server.get("authStatus")
        auth_text = ""
        if isinstance(auth, str):
            auth_text = auth
        elif isinstance(auth, dict):
            auth_text = str(auth.get("status") or auth.get("type") or "")
        status = str(server.get("status") or "ready")
        failure = str(server.get("failureReason") or "")
        bits = [f"{tool_count} tools"] if tool_count else []
        if auth_text:
            bits.append(auth_text)
        if failure:
            bits.append(
                "Reconnect required"
                if failure == "reauthenticationRequired"
                else failure
            )
        row.set_subtitle(" · ".join(bits) or status)
        health = "connected" if status == "ready" else (
            "needs_auth" if failure == "reauthenticationRequired" else (
                "failed" if status in {"failed", "cancelled"} else "unknown"
            )
        )
        icon_name, css = _HEALTH_ICON[health]
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class(css)
        row.add_suffix(icon)
        return row

    # ── account actions ──────────────────────────────────────────────────

    def _set_auth_mode(self, logged_in: bool) -> None:
        """Show exactly one auth action — Sign out when signed in, Sign in
        otherwise — never both at once."""
        self._logged_in = logged_in
        self._auth_btn.set_sensitive(True)
        for c in ("suggested-action", "destructive-action"):
            self._auth_btn.remove_css_class(c)
        if logged_in:
            self._auth_row.set_title("Sign out")
            self._auth_row.set_subtitle(
                "Sign out of your Anthropic account on this machine."
            )
            self._auth_btn.set_label("Sign out")
            self._auth_btn.add_css_class("destructive-action")
        else:
            self._auth_row.set_title("Sign in")
            self._auth_row.set_subtitle("Authenticate with your Anthropic account.")
            self._auth_btn.set_label("Sign in…")
            self._auth_btn.add_css_class("suggested-action")

    def _on_auth_action(self, *_args) -> None:
        if self._logged_in:
            ok, msg = claude_env.launch_logout()
        else:
            ok, msg = claude_env.launch_login("claudeai")
        self._account_group.set_description(
            f"{msg} Then tap refresh ↻." if ok else msg
        )

    def _on_ollama_inputs_changed(self, *_args) -> None:
        self._ollama_check_generation += 1
        self._ollama_status_row.set_subtitle("Settings changed. Save and check to use this server and model.")

    def _on_ollama_check(self, *_args) -> None:
        if self._closed or self._ollama_check_busy:
            return
        try:
            url, model = ollama_titles.validate_config(
                self._ollama_url_row.get_text(), self._ollama_model_row.get_text(),
            )
            ui_state_store().update(ollama_url=url, ollama_title_model=model)
        except (ValueError, OSError) as exc:
            self._ollama_status_row.set_subtitle(str(exc))
            return
        self._ollama_check_generation += 1
        generation = self._ollama_check_generation
        self._ollama_check_busy = True
        self._ollama_check_btn.set_sensitive(False)
        self._ollama_status_row.set_subtitle("Settings saved. Checking model availability…")
        self._run_async(
            lambda: ollama_titles.check_model(url, model),
            lambda result: self._apply_ollama_check(result, generation),
        )

    def _apply_ollama_check(self, result, generation: int) -> None:
        if self._closed:
            return
        self._ollama_check_busy = False
        self._ollama_check_btn.set_sensitive(True)
        if generation != self._ollama_check_generation:
            return
        if isinstance(result, Exception):
            message = f"Settings saved, but connection check failed: {str(result)[:240]}"
        elif result.available:
            message = f"Connected — {result.model} is installed. No text was generated."
        else:
            message = f"Connected, but {result.model} is not installed on this server."
        self._ollama_status_row.set_subtitle(message)

    # ── threading plumbing ───────────────────────────────────────────────

    def _run_async(self, work: Callable[[], object], on_done: Callable[[object], None]) -> None:
        """Run `work()` on a daemon thread; deliver its result to `on_done`
        on the GTK main loop. Exceptions are passed through so callers can
        decide — but our `work` fns already never raise."""
        def runner() -> None:
            try:
                result = work()
            except Exception as e:  # noqa: BLE001 — surfaced to the UI
                result = e
            GLib.idle_add(self._deliver, on_done, result)

        threading.Thread(target=runner, daemon=True).start()

    def _deliver(self, on_done: Callable[[object], None], result: object) -> bool:
        if not self._closed:
            try:
                on_done(result)
            except Exception:  # noqa: BLE001 — never crash the loop on UI update
                pass
        return False  # one-shot

    def _on_closed(self, *_args) -> None:
        self._closed = True
