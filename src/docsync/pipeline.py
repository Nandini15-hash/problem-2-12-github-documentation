"""Orchestrator: the multi-agent workflow, start to finish.

    Watcher  ->  Code agent  ->  Docs agent  ->  Reasoner (LLM)  ->  Verifier  ->  Publisher
    (event)      (API model +     (doc index +     (verdict +         (guardrails    (report, patch,
                  API diff)        static checks)   minimal fix)       G1-G9)         PR / comment / hooks)

Each agent is a plain function over a shared `RunState`. Every step is timed
and written to `trace.jsonl`, so a run can be audited and replayed.
"""
from __future__ import annotations

import difflib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .apidiff import Change, diff_api
from .apimodel import ApiModel, build_api
from .checks import Finding, dedupe, review_candidates, static_checks
from .docindex import DocPage, index_docs
from .guardrails import GuardConfig, verify_fixes
from .reasoner import Reasoner
from .snapshot import Snapshot


@dataclass
class RunConfig:
    package: str
    doc_roots: Sequence[str] = ("docs/", "README.md")
    llm: str = "auto"
    min_confidence: float = 0.7
    fail_on: str = "error"  # error | warning | never
    max_llm_calls: int = 25


@dataclass
class RunState:
    cfg: RunConfig
    base: Optional[Snapshot]
    head: Snapshot
    docs: Snapshot
    base_api: Optional[ApiModel] = None
    head_api: Optional[ApiModel] = None
    changes: List[Change] = field(default_factory=list)
    pages: Dict[str, DocPage] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)
    patched: Dict[str, List[str]] = field(default_factory=dict)
    trace: List[Dict] = field(default_factory=list)
    llm_stats: Dict = field(default_factory=dict)

    def step(self, agent: str, t0: float, **info):
        self.trace.append({"agent": agent, "seconds": round(time.perf_counter() - t0, 3), **info})


def run(state: RunState) -> RunState:
    cfg = state.cfg
    t = time.perf_counter()
    state.head_api = build_api(state.head, cfg.package)
    state.base_api = build_api(state.base, cfg.package) if state.base else None
    state.changes = diff_api(state.base_api, state.head_api) if state.base_api else []
    state.step("code_agent", t, public_symbols=len(state.head_api.public), callables=len(state.head_api.callables),
               api_changes=len(state.changes))

    t = time.perf_counter()
    classes = set(state.head_api.classes) | (set(state.base_api.classes) if state.base_api else set())
    envs = set(state.head_api.env_reads) | (set(state.base_api.env_reads) if state.base_api else set())
    state.pages = index_docs(state.docs, cfg.package, classes, envs, cfg.doc_roots)
    found = static_checks(state.pages, state.head_api, state.base_api)
    found += review_candidates(state.pages, state.head_api, state.changes)
    state.findings = dedupe(found)
    state.step("docs_agent", t, pages=len(state.pages), refs=sum(len(p.refs) for p in state.pages.values()),
               candidates=len(state.findings))

    t = time.perf_counter()
    reasoner = Reasoner(cfg.llm, max_calls=cfg.max_llm_calls)
    reasoner.review(state.findings, state.pages, state.head_api)
    state.llm_stats = {"mode": reasoner.mode, **reasoner.stats}
    state.step("reasoner", t, **state.llm_stats)

    t = time.perf_counter()
    state.patched = verify_fixes(state.findings, state.pages, state.head_api, cfg.package, classes, envs,
                                 GuardConfig(cfg.doc_roots, cfg.min_confidence))
    applied = [f for f in state.findings if f.fix_status == "applied"]
    for f in state.findings:  # one LLM fix often covers several flags in the same span
        if f.verdict == "stale" and not f.fix_status and any(
                a.file == f.file and a.fix["start_line"] <= f.line <= a.fix["end_line"] for a in applied):
            f.fix_status = "applied (shared)"
    state.step("verifier", t, applied=sum(f.fix_status == "applied" for f in state.findings),
               rejected=sum((f.fix_status or "").startswith("rejected") for f in state.findings))
    return state


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------
def confirmed(state: RunState) -> List[Finding]:
    return [f for f in state.findings if f.verdict == "stale"]


def exit_code(state: RunState) -> int:
    sev = {f.severity for f in confirmed(state)}
    if state.cfg.fail_on == "error" and "error" in sev:
        return 1
    if state.cfg.fail_on == "warning" and sev & {"error", "warning"}:
        return 1
    return 0


def unified_patch(state: RunState) -> str:
    out = []
    for file, new in sorted(state.patched.items()):
        old = state.pages[file].lines
        out += difflib.unified_diff([l + "\n" for l in old], [l + "\n" for l in new], f"a/{file}", f"b/{file}")
    return "".join(out)


def render_report(state: RunState) -> str:
    c = confirmed(state)
    head = state.head.label
    rng = f"`{state.base.label}` → `{head}`" if state.base else f"audit of `{head}`"
    L = [f"# DocSync report: {state.cfg.package} ({rng}, docs from `{state.docs.label}`)\n",
         f"**{len(c)} stale doc locations** "
         f"({sum(f.severity == 'error' for f in c)} broken, {sum(f.severity == 'warning' for f in c)} deprecated, "
         f"{sum(f.severity == 'review' for f in c)} contradicting the code). "
         f"Fixes applied: {sum(f.fix_status == 'applied' for f in state.findings)}, suggested: "
         f"{sum(f.fix_status == 'suggestion' for f in state.findings)}, blocked by guardrails: "
         f"{sum((f.fix_status or '').startswith('rejected') for f in state.findings)}. "
         f"Dismissed as still correct: {sum(f.verdict == 'ok' for f in state.findings)}.\n"]
    if state.changes:
        L.append(f"<details><summary>{len(state.changes)} public API changes detected</summary>\n")
        L += [f"- {ch.describe()}" for ch in state.changes[:60]]
        L.append("\n</details>\n")
    L.append("| | where | problem | why (reasoner) | fix |\n|---|---|---|---|---|")
    icon = {"error": "🔴", "warning": "🟠", "review": "🟣"}
    for f in sorted(state.findings, key=lambda f: (f.verdict != "stale", f.file, f.line)):
        if f.verdict == "ok":
            continue
        fx = f.fix_status or ("none" if f.verdict == "stale" else "-")
        L.append(f"| {icon[f.severity]} {f.verdict} | `{f.file}:{f.line}` | {f.message.replace('|', '/')} | "
                 f"{(f.reason or '').replace('|', '/')} | {fx} |")
    ok = [f for f in state.findings if f.verdict == "ok"]
    if ok:
        L.append("\n**Checked and still correct** (no action): " +
                 ", ".join(f"`{f.file}:{f.line}` ({(f.reason or '')[:80]})" for f in ok))
    llm = state.llm_stats
    L.append(f"\n<sub>LLM mode: {llm.get('mode')} · live calls {llm.get('live_calls', 0)} · replayed "
             f"{llm.get('replayed', 0)} · cost ${llm.get('cost_usd', 0):.3f} · pipeline "
             f"{sum(s['seconds'] for s in state.trace):.1f}s</sub>")
    return "\n".join(L) + "\n"


def publish(state: RunState, out_dir: Path) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"report": out_dir / "report.md", "findings": out_dir / "findings.json",
             "patch": out_dir / "docs.patch", "trace": out_dir / "trace.jsonl"}
    paths["report"].write_text(render_report(state), encoding="utf-8")
    paths["findings"].write_text(json.dumps([f.to_dict() for f in state.findings], indent=1, ensure_ascii=False),
                                 encoding="utf-8")
    paths["patch"].write_text(unified_patch(state), encoding="utf-8")
    paths["trace"].write_text("\n".join(json.dumps(s) for s in state.trace) + "\n", encoding="utf-8")
    for file, lines in state.patched.items():
        p = out_dir / "patched" / file
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths
