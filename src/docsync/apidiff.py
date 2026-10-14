"""Code agent, part 2: what changed in the public API between two snapshots."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import List, Optional

from .apimodel import ApiModel


@dataclass
class Change:
    kind: str  # symbol_removed | symbol_added | param_removed | param_added_required | default_changed |
    #            param_deprecated | env_removed | env_added
    symbol: str
    param: Optional[str] = None
    before: Optional[str] = None
    after: Optional[str] = None
    hint: Optional[str] = None  # e.g. "renamed to proxy?"
    evidence: List[str] = field(default_factory=list)

    def describe(self) -> str:
        s = f"{self.kind}: {self.symbol}" + (f"({self.param})" if self.param else "")
        if self.before is not None or self.after is not None:
            s += f"  [{self.before} -> {self.after}]"
        if self.hint:
            s += f"  hint: {self.hint}"
        return s


def diff_api(base: ApiModel, head: ApiModel) -> List[Change]:
    out: List[Change] = []
    for q, b in base.callables.items():
        h = head.callables.get(q)
        if h is None:
            if q.split(".")[0] in base.public or "." in q:
                out.append(Change("symbol_removed", q, evidence=[f"{b.file}:{b.line}"]))
            continue
        for pname, bp in b.params.items():
            if pname not in h.params:
                similar = difflib.get_close_matches(pname, [p for p in h.params if p not in b.params] or list(h.params),
                                                    n=1, cutoff=0.75)
                out.append(Change("param_removed", q, pname, hint=f"renamed to `{similar[0]}`?" if similar else None,
                                  evidence=[f"{h.file}:{h.line}"]))
            elif bp.default != h.params[pname].default and bp.kind != "varkw":
                out.append(Change("default_changed", q, pname, bp.default, h.params[pname].default,
                                  evidence=[f"{h.file}:{h.line}"]))
        for pname, hp in h.params.items():
            if pname not in b.params and hp.required:
                out.append(Change("param_added_required", q, pname, evidence=[f"{h.file}:{h.line}"]))
        for pname, cond in h.deprecated.items():
            if pname not in b.deprecated:
                out.append(Change("param_deprecated", q, pname, after=cond, evidence=[f"{h.file}:{h.line}"]))
    for q in head.callables:
        if q not in base.callables and "." not in q and q in head.public:
            out.append(Change("symbol_added", q))
    for var in sorted(set(base.env_reads) - set(head.env_reads)):
        out.append(Change("env_removed", var, evidence=[f"{f}:{l}: {c}" for f, l, c in base.env_reads[var][:2]]))
    for var in sorted(set(head.env_reads) - set(base.env_reads)):
        out.append(Change("env_added", var, evidence=[f"{f}:{l}: {c}" for f, l, c in head.env_reads[var][:2]]))
    return out
