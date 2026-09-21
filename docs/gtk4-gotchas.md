# GTK4 + PyGObject Gotchas

A field log of the non-obvious failure modes Helios has hit while being built
with Python + GTK4 + libadwaita + GtkSourceView5. Read this before adding a
new widget that touches async I/O, theme tracking, or reparenting.

---

## 1. Bound-method callbacks get garbage-collected mid-async

**Symptom.** You schedule a GIO async op (`read_line_async`,
`communicate_utf8_async`, `wait_async`, …) with `self._on_done` as the
callback. The op never completes. No error, no warning — the read just
silently hangs.

**Cause.** PyGObject's C wrapper for these calls holds only a *weak*
reference to the Python callable. `self._on_done` creates a fresh bound-method
object every time you reference it; when the line returns, Python has no
strong ref and the bound method gets collected. The C code still has its
function pointer but the Python object underneath is gone.

**Fix.** Pin the callbacks as instance attributes in `__init__`:

```python
def __init__(self):
    super().__init__()
    self._cb_done = self._on_done   # pin once
    # later:
    proc.communicate_utf8_async(None, None, self._cb_done, None)
```

**Where Helios hit this:** [backend/process/cli_driver.py](../src/helios/backend/process/cli_driver.py), [backend/process/title_generator.py](../src/helios/backend/process/title_generator.py). Cost ~30 minutes the first
time, identified faster the second.

---

## 2. Reparent assertion: `gtk_widget_get_parent (child) == NULL`

**Symptom.** GTK prints a warning like
`adw_clamp_set_child: assertion 'gtk_widget_get_parent (child) == NULL' failed`
and the second container is missing its child.

**Cause.** You set widget `X` as the child of container `A`, then later
attach it to container `B` without first removing it from `A`. GTK4 is
strict — widgets have exactly one parent.

**Fix.** Either:
- Keep one container, swap its children (`scroller.set_child(self._list)`
  vs `scroller.set_child(self._empty)`), OR
- Explicitly unparent before reattaching.

**Where Helios hit this:** `TranscriptView` swapping a Clamp into the
scroller; `SessionList` swapping listbox vs empty-state label; `Composer`
wrapping a scroller in an Overlay after already adding it to a Box.

**Tell-tale:** the warning fires from `gtk_*_set_child` or
`adw_*_set_child`.

---

## 3. Hover popovers dismiss when you move INTO them

**Symptom.** You build a popover with a button/link inside, trigger it on
`EventControllerMotion::enter` and dismiss on `leave`. The user can never
actually click the inner link — moving the pointer down to it counts as
leaving the trigger widget.

**Fix.** Track hover state on BOTH the trigger and the popover surface.
Maintain a `_hover_count`. Each `enter` increments, each `leave`
decrements + schedules a delayed popdown (200ms grace). When count is
non-zero again before the grace expires, cancel the popdown.

**Where Helios hit this:** [widgets/chat_toolbar.py](../src/helios/widgets/chat_toolbar.py)'s context-meter hover popover.

```python
def _on_hover_enter(self, *_):
    self._hover_count += 1
    if self._popdown_timer_id:
        GLib.source_remove(self._popdown_timer_id)
        self._popdown_timer_id = 0
    if not self._popover.is_visible():
        self._popover.popup()

def _on_hover_leave(self, *_):
    self._hover_count = max(0, self._hover_count - 1)
    if self._hover_count == 0 and self._popdown_timer_id == 0:
        self._popdown_timer_id = GLib.timeout_add(220, self._maybe_popdown)
```

---

## 4. Singleton signals leak per-instance closures

**Symptom.** Your widget connects to `Adw.StyleManager::notify::dark` so it
can re-style on dark/light changes. After many create/destroy cycles
(streaming chat re-renders the assistant bubble per delta) RAM grows.

**Cause.** `Adw.StyleManager` is a process-wide singleton. Each
`sm.connect("notify::dark", self._on_change)` registers a closure that
holds `self`. Even if you `disconnect` in `destroy`, GTK4 doesn't reliably
fire `destroy` for widgets removed via `parent.remove()`.

**Fix.** Centralize. ONE module-level handler walks a `WeakSet` of
subscribers:

```python
_THEME_SUBSCRIBERS: weakref.WeakSet[MyWidget] = weakref.WeakSet()
_THEME_LISTENER_INSTALLED = False

def _on_theme_changed(*_):
    for w in list(_THEME_SUBSCRIBERS):
        try:
            w.restyle()
        except Exception:
            pass

def _ensure_listener():
    global _THEME_LISTENER_INSTALLED
    if _THEME_LISTENER_INSTALLED: return
    Adw.StyleManager.get_default().connect("notify::dark", _on_theme_changed)
    _THEME_LISTENER_INSTALLED = True

class MyWidget(Gtk.Widget):
    def __init__(self):
        super().__init__()
        _ensure_listener()
        _THEME_SUBSCRIBERS.add(self)
```

When the widget is collected (proper GTK destruction → refcount → drop),
the WeakSet entry vanishes. No manual disconnect dance.

**Where Helios hit this:** [widgets/code_block.py](../src/helios/widgets/code_block.py).

---

## 5. Right-click popovers accumulate when not unparented

**Symptom.** Right-click a row → context menu appears. Click outside,
right-click again. Memory grows; eventually GTK warnings about widgets
attached to a closed parent.

**Cause.** `PopoverMenu.set_parent(widget)` parents the popover to the
widget. `popdown()` hides it but doesn't unparent. Without an explicit
`unparent()`, every right-click adds another attached-but-hidden popover.

**Fix.**

```python
popover.connect("closed", lambda p: p.unparent())
```

or reuse a single per-row popover.

**Where Helios hit this:** [widgets/session_list.py](../src/helios/widgets/session_list.py) `_SessionRow` and, before it was folded into the session list, the former `widgets/project_sidebar.py` `_ProjectRow`.

---

## 6. `Gtk.OutputStream.write_bytes` is not write-all

**Symptom.** Send a huge prompt (hundreds of KB), claude appears to hang.
The protocol expects a full JSON line but only got a fragment.

**Cause.** `Gtk.OutputStream.write_bytes` may return after a partial write.
The remaining bytes are silently lost.

**Fix.** Use `write_all` (sync, blocks until complete) or `write_all_async`.

```python
self._stdin.write_all(line, None)
```

**Where Helios hit this:** [backend/process/cli_driver.py](../src/helios/backend/process/cli_driver.py) `send_user_text`.

---

## 7. `Gio.Subprocess.get_exit_status` asserts when signal-killed

**Symptom.** Process killed via `force_exit()` (SIGKILL). Your
`wait_async` callback gets the proc back; calling
`proc.get_exit_status()` raises with a GLib-CRITICAL "WIFEXITED" assertion.

**Cause.** `get_exit_status` requires the process to have exited NORMALLY
(WIFEXITED). Signal-killed processes have WIFSIGNALED instead.

**Fix.** Check first:

```python
if proc.get_if_exited():
    code = proc.get_exit_status()
elif proc.get_if_signaled():
    code = 128 + proc.get_term_sig()
else:
    code = -1
```

**Where Helios hit this:** [backend/process/cli_driver.py](../src/helios/backend/process/cli_driver.py) `_on_exit`.

---

## 8. Pango markup attribute escaping is your job

**Symptom.** A markdown link with an unusual URL like
`[x](https://e.com/" foreground="red)` either crashes Pango or renders
with the wrong colors.

**Cause.** When `Gtk.Label.set_use_markup(True)`, the label parses its
content as Pango markup. Any `"` in your attribute values breaks out of
the attribute.

**Fix.** Escape `"` AND `'` in attribute values. The bulk text escaping
(`&<>`) doesn't cover this. Also block dangerous schemes (`javascript:`,
`data:`, `file:`) — `Gtk.Label` link activation hands the URL to
`xdg-open` which will gladly launch them.

```python
url_attr = url.replace('"', '&quot;').replace("'", '&apos;')
if url.lower().startswith(('javascript:', 'data:', 'vbscript:', 'file:')):
    return plain_text  # no link
return f'<a href="{url_attr}">{label}</a>'
```

**Where Helios hit this:** [widgets/markdown.py](../src/helios/widgets/markdown.py) `_inline_markup.link_repl`.

---

## 9. Headless test construction can segfault

**Symptom.** Run `python3 -c "from main_window import MainWindow; MainWindow(app)"`
in a shell session — segfault, no traceback. The actual app launcher works
fine.

**Cause.** Some widget classes (especially `GtkSource.View`) require a real
GDK display context during early initialization. Construction
outside a properly-staged `Adw.Application.run()` lifecycle is a
documented dragon.

**Fix.** Don't try to construct top-level widgets in shell test scripts.
Test through `Adw.Application` lifecycle, or smoke-test individual
widgets one at a time. The actual app launcher (`./scripts/helios`) works
through the proper init sequence.

---

## 10. `Gtk.ListBox` is fine until ~500 rows

**Symptom.** Project switch starts feeling laggy at hundreds of sessions.

**Cause.** `Gtk.ListBox` is row-based; every row is a fully-instantiated
widget tree. At ~1000 rows the listbox construction itself is the cost.

**Fix.** Migrate to `Gtk.ListView` + `Gio.ListStore` + `Gtk.SignalListItemFactory`.
ListView virtualizes — only visible rows are instantiated. Sort/filter via
`Gtk.SortListModel` / `Gtk.FilterListModel` chains.

**Helios trigger:** when a real project hits >500 sessions. Until then,
the simpler ListBox API is worth more than the virtualization. Workplan
3D.4 tracks this.

---

## 11. Auto-scroll fights you if you call scroll-to-end synchronously

**Symptom.** Streaming chat — you append text, then call
`adj.set_value(adj.get_upper() - adj.get_page_size())`. The view jitters or
sits one frame behind reality.

**Cause.** GTK4 lays out asynchronously. Reading `adj.get_upper()` right
after `append` returns the OLD upper bound — the new widget hasn't been
measured yet.

**Fix.** Subscribe to the adjustment's `changed` signal (fires after
layout reaches a new size). If the user was at the bottom before (within
N pixels), scroll-to-end on `changed`. This is the standard chat-app
"sticky bottom" pattern.

```python
adj = scroller.get_vadjustment()
adj.connect("value-changed", self._on_user_scrolled)
adj.connect("changed", self._on_content_size_changed)

def _on_user_scrolled(self, adj):
    dist = adj.get_upper() - adj.get_page_size() - adj.get_value()
    self._pinned = dist <= 60

def _on_content_size_changed(self, adj):
    if self._pinned:
        GLib.idle_add(lambda: (adj.set_value(adj.get_upper() - adj.get_page_size()), False)[1])
```

**Where Helios hit this:** [widgets/transcript_view.py](../src/helios/widgets/transcript_view.py).

---

## 12. CSS `overflow` is not supported

**Symptom.** Theme parser warning: `<data>:NN:N: No property named "overflow"`.

**Cause.** GTK4's CSS subset doesn't implement `overflow`. Border-radius
clipping is automatic on `Gtk.Box` etc. without needing it.

**Fix.** Just remove the rule.

---

## 13. CSS f-string null bytes (Helios-specific, but instructive)

**Symptom.** Cannot import the module: `SyntaxError: source code string cannot
contain null bytes`. No visible issue in the editor.

**Cause.** The Write tool's payload escaping replaced spaces with `\x00` in
specific f-string contexts. The file looked fine but bytes 0x00 were embedded.

**Fix.** Use a unique-character placeholder (`\x01PH{i}\x01`, or a textual
sentinel like `__HELIOS_PH_{i}__`) instead of space-padded numeric.

---

## 14. Never `del sys.modules['gi']` — it segfaults the next widget

**Symptom.** The whole suite passes module-by-module, but running it in ONE
process on a GTK-capable box segfaults deep in `gi/_gi` — with the Python
frame pointing at an innocent `super().__init__()` of some later widget
(Helios saw it at `PlanPane`/`ActivityIndicator` construction). Invisible in
gi-less CI (`python:3.13-slim`), fatal locally.

**Cause.** A "is this module import-clean?" test tried to prove `mission_store`
has no GTK dependency by wiping `gi` from `sys.modules` and re-importing:

```python
for mod in list(sys.modules):
    if mod == "gi" or mod.startswith("gi."):
        del sys.modules[mod]          # ← the landmine
import helios.backend.mission_store
assert "gi" not in sys.modules
```

`gi._gi` registers the GObject type system **process-globally** in C. Deleting
the Python modules doesn't unregister anything; the next `import gi` (e.g. a
later `from gi.repository import Gtk`) builds a *second* set of Python wrappers
over the *already-registered* C types. Constructing any widget then marshals
through a mismatched wrapper and corrupts the heap. You cannot un-import `gi`.

**Fix.** Assert import-cleanliness in a **fresh subprocess**, never by mutating
the live interpreter — this also reproduces the CI (slim) condition exactly:

```python
result = subprocess.run(
    [sys.executable, "-c",
     "import sys, helios.backend.mission_store as m; "
     "assert not [x for x in sys.modules if x=='gi' or x.startswith('gi.')]"],
    env={**os.environ, "PYTHONPATH": str(SRC)}, capture_output=True, text=True)
assert result.returncode == 0, result.stderr
```

**Where Helios hit this:** `tests/test_mission_store.py::test_no_gi_import`
(H0.2). It poisoned every later GTK test in the same process; the crash moved
around because it only shows at the next `gi` allocation.

**Running the GTK slice safely on a developer desktop:** the real desktop's `gi`/GTK stack
is process-global too, so drive it under an isolated display and HOME —
`HOME=$(mktemp -d) HELIOS_STATE_DIR=$HOME/.helios TANDEM_STATE_DIR=$HOME/.tandem
xvfb-run -a python3 -m pytest -q` — never against the live Wayland session or
`~/.helios`.

---

## 15. AlertDialog does not support Dialog's explicit width setter

**Symptom.** `Adw.AlertDialog.new("Question", "Body").set_content_width(760)`
segfaults on Ubuntu 24.04 / libadwaita 1.5.0, even as a standalone widget under
Xvfb. CI reproduced this in the native-question regression added for v0.95.4;
a desktop with libadwaita 1.9 did not crash, but also kept the alert's narrow layout.

**Fix.** Let `Adw.AlertDialog` size itself. For full-width approval details,
use `Adw.Dialog` with explicit content dimensions, a scrollable content area,
and separate consent actions. A `getattr` check proves the inherited method
exists, not that the alert supports using it.

---

## What we haven't hit but should remember

- **Threads:** never touch a GTK widget off the main thread. Marshal back
  with `GLib.idle_add(callback, ...)`.
- **CSS scoping:** GTK CSS is global. Use CSS classes liberally; we use
  the `helios-` prefix everywhere to avoid clobbering libadwaita defaults.
- **Adw.AlertDialog:** call `.present(window)`, not `.show()`. The
  argument is the parent window or any widget within it.
- **Action groups on widgets:** use `widget.insert_action_group("prefix",
  group)`. Actions bound to a widget go out of scope when the widget is
  destroyed — usually what you want.
