import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EX = ROOT / "examples" / "httpx"


@pytest.fixture(scope="session")
def snaps():
    from src.docsync.snapshot import Snapshot
    return {t: Snapshot.from_dir(EX / t, t) for t in ("0.27.2", "0.28.0", "0.28.1")}


@pytest.fixture(scope="session")
def apis(snaps):
    from src.docsync.apimodel import build_api
    return {t: build_api(s, "httpx") for t, s in snaps.items()}
