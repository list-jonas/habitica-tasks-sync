"""Shared test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running `pytest` from the repo root without an editable install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
