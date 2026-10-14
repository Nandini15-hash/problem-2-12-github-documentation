"""Entry point: `python src/main.py <command> ...` (same as `python -m src.<pkg>.cli`)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, so `src.*` imports work

from src.docsync.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
