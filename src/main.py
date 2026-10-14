"""Entry point: `python src/main.py <command> ...` (same as `python -m src.<pkg>.cli`)."""
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):  # Windows consoles/pipes default to cp1252
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, so `src.*` imports work

from src.docsync.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
