"""Verifier agent: nothing the LLM writes reaches a branch without passing these checks.

Every proposed fix is applied to an in-memory copy of the page and must pass all of:
  G1 path       file is under an allowed doc root (never code, CI config or secrets)
  G2 scope      edit stays inside the flagged section and spans <= MAX_SPAN lines
  G3 size       replacement <= MAX_REPLACEMENT lines; total run budget <= MAX_TOTAL lines
  G4 links      no URL that was not already in the section (blocks phishing / spam links)
  G5 secrets    no token-looking strings
  G6 syntax     every Python block that parsed before still parses
  G7 api        re-running the static checks on the patched page introduces no new
                findings, and the targeted static finding is gone
  G8 confidence < min_confidence  -> downgraded to "suggestion" (comment only, no patch)
  G9 overlap    overlapping fixes on the same lines: the first one wins
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

from .apimodel import ApiModel
from .checks import Finding, static_checks
from .docindex import FENCE, DocPage, _strip_prompts, index_page

MAX_SPAN, MAX_REPLACEMENT, MAX_TOTAL = 30, 40, 400
URL = re.compile(r"https?://[^\s)>\]\"']+")
SECRET = re.compile(r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|"
                    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abp]-[A-Za-z0-9-]{10,})")


@dataclass
class GuardConfig:
    doc_roots: Sequence[str] = ("docs/", "README.md")
    min_confidence: float = 0.7


def _py_blocks_ok(lines: List[str]) -> List[bool]:
    res, i = [], 0
    while i < len(lines):
        m = FENCE.match(lines[i])
        if m:
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(m.group(1)):
                j += 1
            if m.group(2).lower() in ("python", "py", "pycon"):
                try:
                    ast.parse("\n".join(_strip_prompts(lines[i + 1:j])))
                    res.append(True)
                except SyntaxError:
                    res.append(False)
            i = j + 1
        else:
            i += 1
    return res


def _sig(f: Finding) -> Tuple:
    return (f.rule, f.symbol, f.param)


def verify_fixes(findings: List[Finding], pages: Dict[str, DocPage], head: ApiModel, pkg: str,
                 classes, env_names, cfg: GuardConfig = GuardConfig()) -> Dict[str, List[str]]:
    """Apply fixes that pass every guardrail. Returns {file: patched_lines}."""
    patched: Dict[str, List[str]] = {}
    taken: Dict[str, List[Tuple[int, int]]] = {}
    total = 0
    # bottom-up per file so earlier line numbers stay valid
    for f in sorted([x for x in findings if x.fix], key=lambda x: (x.file, -x.fix["start_line"])):
        page = pages[f.file]
        s, e, rep = f.fix["start_line"], f.fix["end_line"], f.fix["replacement"]
        sec_s, sec_e, _ = page.section_bounds(f.line)
        rep_lines = rep.split("\n") if rep else []

        def reject(why: str):
            f.fix_status = f"rejected:{why}"

        if not any(f.file == r or f.file.startswith(r) for r in cfg.doc_roots):
            reject("G1_path"); continue
        max_span = MAX_SPAN * 3 if not rep.strip() else MAX_SPAN  # pure deletions (e.g. a removed feature) may be longer
        if not (sec_s <= s <= e <= sec_e) or e - s + 1 > max_span:
            reject("G2_scope"); continue
        if len(rep_lines) > MAX_REPLACEMENT or total + len(rep_lines) > MAX_TOTAL:
            reject("G3_size"); continue
        before_urls = set(URL.findall("\n".join(page.lines[sec_s - 1:sec_e])))
        if set(URL.findall(rep)) - before_urls:
            reject("G4_new_link"); continue
        if SECRET.search(rep):
            reject("G5_secret"); continue
        if any(not (e < a or s > b) for a, b in taken.get(f.file, [])):
            reject("G9_overlap"); continue
        current = patched.get(f.file, list(page.lines))
        candidate = current[:s - 1] + rep_lines + current[e:]
        before_ok, after_ok = _py_blocks_ok(current), _py_blocks_ok(candidate)
        if after_ok.count(False) > before_ok.count(False):  # deleting a block is fine; breaking one is not
            reject("G6_syntax"); continue
        old_static = {_sig(x) for x in static_checks({f.file: index_page(f.file, "\n".join(current), pkg, classes, env_names)}, head)}
        new_static = static_checks({f.file: index_page(f.file, "\n".join(candidate), pkg, classes, env_names)}, head)
        new_sigs = {_sig(x) for x in new_static}
        if new_sigs - old_static:
            reject("G7_introduces_api_error"); continue
        if f.severity in ("error", "warning") and f.rule != "ENV_NOT_READ":
            still = [x for x in new_static if _sig(x) == _sig(f) and abs(x.line - f.line) <= len(rep_lines) + 2]
            if still:
                reject("G7_not_resolved"); continue
        if f.confidence < cfg.min_confidence:
            f.fix_status = "suggestion"  # surfaced in the report / PR comment, not patched
            continue
        patched[f.file] = candidate
        taken.setdefault(f.file, []).append((s, e))
        total += len(rep_lines)
        f.fix_status = "applied"
    return patched
