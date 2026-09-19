"""Single source of truth for Helios transition durations.

`helios.css` cannot reference these. Ubuntu 24.04 ships GTK 4.14; CSS custom
properties / `var()` landed in 4.16, and `@define-color` is colors only. So
"declare the duration once" is achievable only as a Python constant for the
Python side plus normalised literals in the stylesheet.

**The stylesheet does NOT yet mirror these values.** It still carries
140/160/170/180/200/220/240/260/420/440, of which only 140 and 220 are in
`DURATIONS`, and nothing pins the two in sync — `tests/test_style_tokens.py`
guards the Python side only. Reconciling the sheet is M-3b, which has to land
last in the stylesheet queue because it rewrites every literal in the file and
would invalidate the line citations of every other pending CSS item.

Pure ints: no `import gi`, so this is importable from the `python:3.13-slim`
`tests` lane, and `test_style_tokens.py` imports it to prove exactly that.
"""

from __future__ import annotations

FAST_MS = 140  # micro-feedback: the context-pane page switch
BASE_MS = 220  # everything else: revealers, hovers, stack and label crossfades
SLOW_MS = 320  # deliberate: the context arc sweep (arrives with M-3b's 420/440)

DURATIONS = (FAST_MS, BASE_MS, SLOW_MS)
