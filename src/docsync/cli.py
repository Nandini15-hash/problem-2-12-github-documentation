"""DocSync command line.

  scan     --repo PATH --base REF --head REF [--docs-ref REF]        (live git repo)
           --base-dir DIR --head-dir DIR [--docs-dir DIR]             (plain directories)
  demo     run both httpx scenarios from the vendored snapshots -> out/demo/
  eval     full evaluation -> eval/results.md
  ask      "how do I set a timeout"     search the current API (docs concierge)
  check    FILE|-                       check a code snippet against the current API
  mcp      MCP stdio server for IDE agents
  serve    inbound webhook server (GitHub, generic POST /scan)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .apimodel import build_api
from .concierge import Concierge
from .connectors import open_pull_request, verify_github_signature, webhook_notifier
from .docindex import index_docs
from .pipeline import RunConfig, RunState, exit_code, publish, run
from .snapshot import Snapshot

ROOT = Path(__file__).resolve().parents[2]
EX = ROOT / "examples" / "httpx"


def _snap(repo, ref, d, label=None):
    if d:
        return Snapshot.from_dir(Path(d), label or Path(d).name)
    if repo and ref:
        return Snapshot.from_git(Path(repo), ref)
    return None


def cmd_scan(a) -> int:
    base = _snap(a.repo, a.base, a.base_dir)
    head = _snap(a.repo, a.head, a.head_dir)
    docs = _snap(a.repo, a.docs_ref, a.docs_dir) or head
    if head is None:
        sys.exit("need --head (with --repo) or --head-dir")
    cfg = RunConfig(a.package, tuple(a.doc_roots), a.llm, a.min_confidence, a.fail_on, a.max_llm_calls)
    st = run(RunState(cfg, base, head, docs))
    paths = publish(st, Path(a.out))
    print(paths["report"].read_text(encoding="utf-8"))
    print(f"[docsync] wrote {', '.join(str(p) for p in paths.values())}")
    if a.notify_webhook:
        webhook_notifier(a.notify_webhook)(st, paths)
    if a.open_pr:
        for c in open_pull_request(st, paths, Path(a.repo or "."), dry_run=not a.yes):
            print(("[dry-run] " if not a.yes else "") + c)
    return exit_code(st)


def cmd_demo(a) -> int:
    for name, (b, h, d) in {"replay-0.28.0": ("0.27.2", "0.28.0", "0.27.2"),
                            "audit-0.28.1": ("0.28.0", "0.28.1", "0.28.1")}.items():
        st = run(RunState(RunConfig("httpx", llm=a.llm), Snapshot.from_dir(EX / b, b), Snapshot.from_dir(EX / h, h),
                          Snapshot.from_dir(EX / d, d)))
        paths = publish(st, ROOT / "out" / "demo" / name)
        print(f"\n===== {name} =====")
        print(paths["report"].read_text(encoding="utf-8"))
        print(f"patch: {paths['patch']}")
    return 0


def _concierge(a) -> Concierge:
    snap = _snap(a.repo, a.head, a.head_dir) or Snapshot.from_dir(EX / "0.28.1", "0.28.1")
    api = build_api(snap, a.package)
    pages = index_docs(snap, a.package, set(api.classes), set(api.env_reads))
    return Concierge(api, pages)


def cmd_ask(a) -> int:
    c = _concierge(a)
    for card in c.search(" ".join(a.query), a.k):
        print(card.render() + "\n")
    return 0


def cmd_check(a) -> int:
    code = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
    issues = _concierge(a).check_snippet(code)
    print(json.dumps(issues, indent=1) if issues else "OK: no issues against the current API")
    return 1 if (issues and a.strict) else 0


def cmd_mcp(a) -> int:
    from .mcp_server import serve_stdio
    serve_stdio(_concierge(a))
    return 0


def cmd_serve(a) -> int:
    secret = os.environ.get("DOCSYNC_WEBHOOK_SECRET", "")

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            self._send(200 if self.path == "/healthz" else 404, {"status": "ok"})

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/webhook/github":
                if not verify_github_signature(secret, body, self.headers.get("X-Hub-Signature-256", "")):
                    return self._send(401, {"error": "bad signature"})
                ev = json.loads(body)
                pr = ev.get("pull_request") or {}
                job = {"repo": a.repo, "base": pr.get("base", {}).get("sha") or ev.get("before"),
                       "head": pr.get("head", {}).get("sha") or ev.get("after")}
            elif self.path == "/scan":
                if secret and self.headers.get("Authorization") != f"Bearer {secret}":
                    return self._send(401, {"error": "unauthorized"})
                job = json.loads(body)
                job.setdefault("repo", a.repo)
            else:
                return self._send(404, {"error": "not found"})

            def work():  # scans run off the request thread; results go to the configured sinks
                ns = argparse.Namespace(**{**vars(a), "base": job.get("base"), "head": job.get("head"),
                                           "repo": job.get("repo"), "docs_ref": job.get("head"),
                                           "base_dir": None, "head_dir": None, "docs_dir": None,
                                           "out": str(Path(a.out) / str(job.get("head"))[:12])})
                cmd_scan(ns)
            threading.Thread(target=work, daemon=True).start()
            self._send(202, {"accepted": job})

        def log_message(self, *x):
            pass

    print(f"[docsync] webhook server on :{a.port} (POST /webhook/github, POST /scan, GET /healthz)")
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="docsync")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, scan=True):
        sp.add_argument("--package", default="httpx")
        sp.add_argument("--repo")
        sp.add_argument("--head")
        sp.add_argument("--head-dir")
        if scan:
            sp.add_argument("--base")
            sp.add_argument("--docs-ref")
            sp.add_argument("--base-dir")
            sp.add_argument("--docs-dir")
            sp.add_argument("--doc-roots", nargs="+", default=["docs/", "README.md"])
            sp.add_argument("--llm", default="auto", choices=["auto", "live", "replay", "off"])
            sp.add_argument("--min-confidence", type=float, default=0.7)
            sp.add_argument("--fail-on", default="error", choices=["error", "warning", "never"])
            sp.add_argument("--max-llm-calls", type=int, default=25)
            sp.add_argument("--out", default=str(ROOT / "out" / "scan"))
            sp.add_argument("--notify-webhook")
            sp.add_argument("--open-pr", action="store_true")
            sp.add_argument("--yes", action="store_true", help="really push/open the PR (default: dry run)")

    common(sub.add_parser("scan"))
    d = sub.add_parser("demo")
    d.add_argument("--llm", default="auto", choices=["auto", "live", "replay", "off"])
    e = sub.add_parser("eval")
    e.add_argument("--llm", default="replay", choices=["auto", "live", "replay", "off"])
    q = sub.add_parser("ask")
    common(q, scan=False)
    q.add_argument("query", nargs="+")
    q.add_argument("-k", type=int, default=3)
    c = sub.add_parser("check")
    common(c, scan=False)
    c.add_argument("file")
    c.add_argument("--strict", action="store_true", help="exit 1 when issues are found (for pre-commit hooks)")
    m = sub.add_parser("mcp")
    common(m, scan=False)
    s = sub.add_parser("serve")
    common(s)
    s.add_argument("--port", type=int, default=8787)
    a = p.parse_args(argv)
    if a.cmd == "eval":
        from .evaluate import main as ev
        ev(a.llm)
        return 0
    return {"scan": cmd_scan, "demo": cmd_demo, "ask": cmd_ask, "check": cmd_check, "mcp": cmd_mcp,
            "serve": cmd_serve}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
