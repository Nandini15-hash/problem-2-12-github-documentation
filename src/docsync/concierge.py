"""Docs concierge: serve documentation that is generated from the code, so it cannot drift.

Instead of developers reading pages that may be stale, they (or their IDE agent) ask:
  search_api("set a timeout")    -> API cards ranked by BM25, generated from HEAD
  get_symbol("Client")            -> signature, docstring, deprecations + migration text,
                                     and *verified* examples: doc snippets that pass the
                                     static checks against HEAD (stale ones are excluded)
  check_snippet(code)             -> "your code uses cert=, deprecated since ...; use ..."
Exposed via CLI and as an MCP server (mcp_server.py), so Claude Code, Cursor and similar tools can call it.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .apimodel import ApiModel
from .checks import static_checks
from .docindex import DocPage, index_page

_TOK = re.compile(r"[a-z0-9]+")
STOP = {"how", "do", "i", "a", "an", "the", "to", "can", "with", "my", "set", "use", "using", "make", "what", "is",
        "are", "in", "of", "for", "on", "it", "me", "should", "does", "when", "and", "or", "you"}


def _tokens(text: str) -> List[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).replace("_", " ")
    return _TOK.findall(text.lower())


@dataclass
class Card:
    symbol: str
    signature: str
    summary: str
    deprecations: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)  # "file:line  code"
    source: str = ""

    def render(self) -> str:
        out = [f"### {self.symbol}", f"`{self.signature}`  ({self.source})", self.summary]
        if self.deprecations:
            out += ["**Deprecated:** " + d for d in self.deprecations]
        if self.examples:
            out += ["Verified examples (pass checks against current code):"] + [f"    {e}" for e in self.examples]
        return "\n".join(x for x in out if x)


class Concierge:
    def __init__(self, api: ApiModel, pages: Dict[str, DocPage]):
        self.api = api
        bad = {(f.file, f.line) for f in static_checks(pages, api)}
        examples: Dict[str, List[str]] = {}
        for p in pages.values():
            for r in p.refs:
                if r.kind == "call" and r.symbol and not r.anti_example and (r.file, r.line) not in bad:
                    root = r.symbol if r.symbol in api.callables else r.symbol.split(".")[0]
                    ex = examples.setdefault(root, [])
                    if len(ex) < 3:
                        ex.append(f"{r.file}:{r.line}  {p.lines[r.line - 1].strip()}")
        self.cards: Dict[str, Card] = {}
        for q, c in api.callables.items():
            if q.split(".")[0] not in api.public:
                continue
            summary = c.doc.split("\n\n")[0].strip()[:400]
            deps = [f"`{p}` (warns when `{cond}`)" for p, cond in c.deprecated.items()]
            deps += api.deprecation_messages.get(q, [])[:2]
            self.cards[q] = Card(q, c.signature(), summary, deps, examples.get(q, []), f"{c.file}:{c.line}")
        self._index()

    def _index(self):
        self.docs = {q: _tokens(q) * 3 + _tokens(c.summary) + _tokens(c.signature) + _tokens(" ".join(c.deprecations))
                     for q, c in self.cards.items()}
        self.df = Counter(t for toks in self.docs.values() for t in set(toks))
        self.avg = sum(len(t) for t in self.docs.values()) / max(1, len(self.docs))

    def search(self, query: str, k: int = 5) -> List[Card]:
        q = [t for t in _tokens(query) if t not in STOP] or _tokens(query)
        n = len(self.docs)
        scores = {}
        for sym, toks in self.docs.items():
            tf = Counter(toks)
            s = 0.0
            for t in q:
                if t not in tf:  # cheap stemming: "certificate" ~ "cert", "timeouts" ~ "timeout"
                    t = next((x for x in tf if len(x) >= 4 and len(t) >= 4 and (x.startswith(t) or t.startswith(x))), t)
                if t in tf:
                    idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                    s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(toks) / self.avg))
            if s:
                scores[sym] = s - 0.01 * sym.count(".")  # prefer top-level entry points on ties
        return [self.cards[s] for s in sorted(scores, key=scores.get, reverse=True)[:k]]

    def get(self, symbol: str) -> Optional[Card]:
        symbol = symbol.replace(f"{self.api.package}.", "")
        return self.cards.get(symbol)

    def check_snippet(self, code: str) -> List[Dict]:
        page = index_page("<snippet>", "```python\n" + code + "\n```", self.api.package, set(self.api.classes),
                          set(self.api.env_reads))
        out = []
        for f in static_checks({"<snippet>": page}, self.api):
            hint = None
            if f.rule == "DEPRECATED_KWARG":
                msgs = self.api.deprecation_messages.get(f.symbol or "", [])
                hint = next((m for m in msgs if m.startswith(f"`{f.param}=")), None) or next(
                    (m for m in msgs if f"{f.param}=" in m.split(".")[0]), None)
            elif f.rule == "UNKNOWN_KWARG":
                alts = [q for q, c in self.api.callables.items()
                        if f.param in c.params and q.split(".")[0] in self.api.public][:4]
                hint = f"`{f.param}` is accepted by: {', '.join(alts)}" if alts else None
            out.append({"line": f.line - 1, "rule": f.rule, "message": f.message, "hint": hint})
        return out
