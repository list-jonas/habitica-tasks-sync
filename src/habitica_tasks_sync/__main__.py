"""Allow `python -m habitica_tasks_sync` to start the daemon."""

from __future__ import annotations

import sys

from .main import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
