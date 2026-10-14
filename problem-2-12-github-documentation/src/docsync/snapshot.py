"""Read-only view of a repository at one point in time (a git ref or a directory).

Every agent works on Snapshots, never on a live checkout, so a run is
reproducible and can never modify the user's working tree by accident.
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import List, Optional


class Snapshot:
    def __init__(self, root: Path, ref: Optional[str] = None, label: Optional[str] = None):
        self.root = Path(root)
        self.ref = ref
        self.label = label or (ref or self.root.name)

    @classmethod
    def from_git(cls, repo: Path, ref: str) -> "Snapshot":
        return cls(Path(repo), ref=ref, label=ref)

    @classmethod
    def from_dir(cls, path: Path, label: Optional[str] = None) -> "Snapshot":
        return cls(Path(path), ref=None, label=label)

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout

    @lru_cache(maxsize=None)
    def files(self) -> List[str]:
        if self.ref:
            return [l for l in self._git("ls-tree", "-r", "--name-only", self.ref).splitlines() if l]
        return sorted(str(p.relative_to(self.root)).replace("\\", "/") for p in self.root.rglob("*")
                      if p.is_file() and ".git" not in p.parts)

    @lru_cache(maxsize=None)
    def read(self, path: str) -> str:
        if self.ref:
            return self._git("show", f"{self.ref}:{path}")
        return (self.root / path).read_text(encoding="utf-8", errors="replace")

    def glob(self, prefix: str = "", suffix: str = "") -> List[str]:
        return [f for f in self.files() if f.startswith(prefix) and f.endswith(suffix)]

    def __repr__(self) -> str:
        return f"Snapshot({self.label})"
