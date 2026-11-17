"""Settings every test session needs before any test module is imported.

Typer decides at import time whether help and error output goes through a terminal: it forces
terminal mode (ANSI colours, boxed panels, terminal-width wrapping) when ``GITHUB_ACTIONS``,
``FORCE_COLOR`` or ``PY_COLORS`` is set. On a CI runner that turns every CLI message the tests
read into styled text, so substring checks that pass locally fail there. Its own switch turns
the detection off, which keeps CLI output identical on every machine.
"""

from __future__ import annotations

import os

os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"

# A developer shell may export a vendor base-URL override (a local proxy, say). Providers
# refuse such an override unless a model entry opts in, so an inherited one would fail every
# test that builds a default vendor client. Tests that exercise the override set it themselves.
for _name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
    os.environ.pop(_name, None)
