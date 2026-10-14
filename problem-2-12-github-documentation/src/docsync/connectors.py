"""Connectors: how DocSync plugs into other systems (open hooks).

Inbound   - GitHub Action (deploy/github-action.yml), a GitHub webhook (HMAC-verified),
            a generic `POST /scan`, or the CLI from any CI system.
Outbound  - PR with the patch (gh CLI), PR comment, generic JSON webhook (Slack/Teams/
            anything), files on disk. New sinks only need the `Notifier` signature.

Security:
  * secrets only come from env vars (GITHUB_TOKEN, DOCSYNC_WEBHOOK_SECRET, CLAUDE_KEY)
    and never appear in reports or traces;
  * inbound webhooks are rejected without a valid X-Hub-Signature-256;
  * outbound actions are dry-run unless explicitly enabled, PRs go to a new branch
    (never the default branch), and only files that passed the guardrails are committed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List

from .pipeline import RunState, confirmed

Notifier = Callable[[RunState, Dict[str, Path]], None]


def summary(state: RunState) -> Dict:
    c = confirmed(state)
    return {"package": state.cfg.package, "head": state.head.label, "base": state.base.label if state.base else None,
            "stale": len(c), "errors": sum(f.severity == "error" for f in c),
            "fixes_applied": sum((f.fix_status or "").startswith("applied") for f in state.findings),
            "top": [f"{f.file}:{f.line} {f.message}" for f in c[:5]]}


def webhook_notifier(url: str) -> Notifier:
    def send(state: RunState, paths: Dict[str, Path]) -> None:
        s = summary(state)
        body = {"text": f"DocSync: {s['stale']} stale doc locations in {s['package']}@{s['head']} "
                        f"({s['fixes_applied']} auto-fixed)\n" + "\n".join("• " + t for t in s["top"]), **s}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)  # nosec - URL is operator-configured
    return send


def open_pull_request(state: RunState, paths: Dict[str, Path], repo: Path, dry_run: bool = True) -> List[str]:
    """Commit the guardrail-approved patch to a new branch and open a PR. Returns the commands run."""
    branch = f"docsync/{state.head.label[:12]}"
    cmds = [["git", "-C", str(repo), "checkout", "-b", branch],
            ["git", "-C", str(repo), "apply", str(paths["patch"].resolve())],
            ["git", "-C", str(repo), "commit", "-am", f"docs: sync with {state.head.label} (DocSync)"],
            ["git", "-C", str(repo), "push", "-u", "origin", branch],
            ["gh", "pr", "create", "--repo", os.environ.get("GITHUB_REPOSITORY", ""), "--head", branch,
             "--title", f"docs: sync documentation with {state.head.label}", "--body-file", str(paths["report"])]]
    if not paths["patch"].read_text().strip():
        return []
    if dry_run or not os.environ.get("GITHUB_TOKEN"):
        return [" ".join(c) for c in cmds]
    for c in cmds:
        subprocess.run(c, check=True, cwd=repo)
    return [" ".join(c) for c in cmds]


def verify_github_signature(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)
