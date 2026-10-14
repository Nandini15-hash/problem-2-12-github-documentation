"""Docs agent, part 2: deterministic staleness checks.

Two families:

* **Static rules** check every doc reference against the API at HEAD. They are
  precise, so they can fail CI on their own:
    STALE_SYMBOL      doc uses `httpx.X` and X no longer exists
    UNKNOWN_KWARG     doc calls X(foo=...) and X has no `foo` (and no **kwargs)
    DEPRECATED_KWARG  doc passes a value the code now warns about
    ENV_NOT_READ      doc documents an env var the code stopped reading
* **Review candidates** are fuzzier and go to the reasoning agent (LLM):
    IMPACT            prose or code touching something that changed in this diff
    ENV_CLAIM         prose makes a negative claim ("does not use SSL_CERT_FILE")
                      about an env var the code *does* read
"""
from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .apidiff import Change
from .apimodel import ApiModel
from .docindex import DocPage, DocRef

SEVERITY = {"STALE_SYMBOL": "error", "UNKNOWN_KWARG": "error", "ENV_NOT_READ": "error",
            "DEPRECATED_KWARG": "warning", "IMPACT": "review", "ENV_CLAIM": "review"}
NEGATION = re.compile(r"\b(not|no longer|doesn't|does not|don't|never|ignores?|isn't|without)\b", re.I)


@dataclass
class Finding:
    file: str
    line: int
    rule: str
    message: str
    symbol: Optional[str] = None
    param: Optional[str] = None
    section: str = ""
    evidence: List[str] = field(default_factory=list)
    confidence: float = 1.0
    related_change: Optional[str] = None
    verdict: Optional[str] = None  # filled by reasoner: stale | ok | unsure
    reason: Optional[str] = None
    fix: Optional[Dict] = None  # {"start_line", "end_line", "replacement"}
    fix_status: Optional[str] = None  # proposed | applied | rejected:<guardrail>

    @property
    def severity(self) -> str:
        return SEVERITY[self.rule]

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.file}:{self.line}:{self.rule}:{self.param or self.symbol}".encode()).hexdigest()[:10]

    def to_dict(self) -> Dict:
        d = asdict(self)
        d.update(id=self.id, severity=self.severity)
        return d


def _literal(src: str):
    try:
        return True, ast.literal_eval(src)
    except Exception:
        return False, None


def deprecation_applies(cond: str, param: str, value_src: str) -> Optional[bool]:
    """Does passing `value_src` for `param` trigger the warning guarded by `cond`?

    Pattern-matched (never `eval`: the condition comes from the analysed repo).
    Returns None when we cannot tell.
    """
    is_lit, val = _literal(value_src)
    m = re.fullmatch(rf"isinstance\(\s*{param}\s*,\s*\(?([\w, ]+)\)?\s*\)", cond.strip())
    if m:
        if not is_lit:  # a variable: we cannot prove its type, and static rules must stay precise
            return False
        names = {n.strip() for n in m.group(1).split(",")}
        return type(val).__name__ in names
    if re.fullmatch(rf"{param}(\s+is\s+not\s+None)?", cond.strip()):
        if not is_lit:  # any real value passed (a variable, a call) trips a truthiness / not-None check
            return True
        return val is not None and (bool(val) or "is not None" in cond)
    return None  # condition too complex to match safely


def static_checks(pages: Dict[str, DocPage], head: ApiModel, base: Optional[ApiModel] = None) -> List[Finding]:
    out: List[Finding] = []
    public_roots = head.public | {q.split(".")[0] for q in head.callables}
    for page in pages.values():
        for r in page.refs:
            if r.kind in ("call", "mention") and r.symbol:
                root = r.symbol.split(".")[0]
                if root not in public_roots and (base is None or root in base.public or root in base.callables):
                    out.append(Finding(r.file, r.line, "STALE_SYMBOL", f"`{r.name}` is not part of the "
                                       f"{head.package} API at {head.label}", symbol=r.symbol, section=r.section))
                    continue
            if r.kind == "call" and r.anti_example:
                continue  # the page is deliberately showing what *not* to do
            if r.kind == "call" and r.symbol in head.callables:
                c = head.callables[r.symbol]
                for k, v in r.kwargs:
                    if k not in c.params and not c.has_varkw:
                        out.append(Finding(r.file, r.line, "UNKNOWN_KWARG",
                                           f"`{r.symbol}()` has no parameter `{k}` at {head.label}",
                                           symbol=r.symbol, param=k, section=r.section,
                                           evidence=[f"signature @ {head.label}: {c.signature()}"]))
                    elif k in c.deprecated:
                        applies = deprecation_applies(c.deprecated[k], k, v)
                        if not applies:  # False, or None = condition we cannot evaluate safely
                            continue
                        out.append(Finding(r.file, r.line, "DEPRECATED_KWARG",
                                           f"`{r.symbol}({k}={v})` triggers a DeprecationWarning at {head.label} "
                                           f"(warns when `{c.deprecated[k]}`)", symbol=r.symbol, param=k,
                                           section=r.section,
                                           evidence=[f"{c.file}:{c.line} {c.signature()}"]))
            if r.kind == "env" and base is not None and r.name in base.env_reads and r.name not in head.env_reads:
                out.append(Finding(r.file, r.line, "ENV_NOT_READ", f"`{r.name}` is no longer read by the code at "
                                   f"{head.label}", param=r.name, section=r.section,
                                   evidence=[f"was read at {base.label}: {f}:{l}: {c}" for f, l, c in base.env_reads[r.name][:2]]))
    return out


def review_candidates(pages: Dict[str, DocPage], head: ApiModel, changes: List[Change]) -> List[Finding]:
    out: List[Finding] = []
    changed_params = {}
    changed_syms = {}
    changed_env = {}
    for ch in changes:
        if ch.param and ch.kind.startswith(("param", "default")):
            changed_params.setdefault(ch.param, []).append(ch)
        elif ch.kind.startswith("symbol"):
            changed_syms.setdefault(ch.symbol, []).append(ch)
        elif ch.kind.startswith("env"):
            changed_env.setdefault(ch.symbol, []).append(ch)
    for page in pages.values():
        for r in page.refs:
            hits: List[Change] = []
            if r.kind == "param_mention":
                hits = changed_params.get(r.name, [])
            elif r.kind == "env":
                hits = changed_env.get(r.name, [])
            elif r.kind in ("call", "mention") and r.symbol:
                hits = list(changed_syms.get(r.symbol, []))
                if r.kind == "call":
                    hits += [c for k, _ in r.kwargs for c in changed_params.get(k, []) if c.symbol == r.symbol
                             and c.kind == "default_changed"]
            for ch in hits[:1]:
                out.append(Finding(r.file, r.line, "IMPACT", f"references `{r.name}`, which changed: {ch.describe()}",
                                   symbol=ch.symbol, param=ch.param, section=r.section, confidence=0.5,
                                   related_change=ch.describe(), evidence=ch.evidence))
            # negative claims about env vars the code reads
            if r.kind == "env" and not r.in_code and r.name in head.env_reads:
                text = page.lines[r.line - 1]
                if NEGATION.search(text):
                    out.append(Finding(r.file, r.line, "ENV_CLAIM", f"prose makes a negative claim about "
                                       f"`{r.name}`, but the code reads it", param=r.name, section=r.section,
                                       confidence=0.5,
                                       evidence=[f"{f}:{l}: {c}" for f, l, c in head.env_reads[r.name][:3]]))
    return out


def dedupe(findings: List[Finding]) -> List[Finding]:
    """One finding per (file, line, param/symbol); static rules beat review candidates."""
    rank = {"error": 0, "warning": 1, "review": 2}
    best: Dict[tuple, Finding] = {}
    for f in sorted(findings, key=lambda f: rank[f.severity]):
        key = (f.file, f.line, f.param or f.symbol)
        best.setdefault(key, f)
    return sorted(best.values(), key=lambda f: (f.file, f.line))
