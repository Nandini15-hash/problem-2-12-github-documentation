"""Docs agent, part 1: index every place the documentation touches the API.

For each Markdown file we extract:
  * calls in Python code blocks (`httpx.Client(cert=...)`, `client.get(url, timeout=5)`),
    resolving variables bound earlier on the page (`client = httpx.Client()`,
    `with httpx.AsyncClient() as client:`) to their class;
  * prose mentions of symbols (`httpx.get`), keyword params (`proxies=...`) and
    environment variables the code reads (`SSL_CERT_FILE`);
  * the section heading each reference sits under, so findings can be grouped
    and the LLM sees a coherent chunk instead of a single line.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .snapshot import Snapshot

FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)")
PY_LANGS = {"python", "py", "pycon", "python3", ""}
ANTI = re.compile(r"\b(not supported|not allowed|don't|do not|avoid|wrong|incorrect|instead of|deprecated|no longer)\b", re.I)
SHELL_LANGS = {"console", "bash", "shell", "sh", "shell-session", "text"}


@dataclass
class DocRef:
    file: str
    line: int
    kind: str  # call | mention | param_mention | env
    name: str  # raw text, e.g. "httpx.Client" / "proxies" / "SSL_CERT_FILE"
    symbol: Optional[str] = None  # resolved API qualname, e.g. "Client" or "Client.get"
    kwargs: List[Tuple[str, str]] = field(default_factory=list)  # (name, value source)
    section: str = ""
    in_code: bool = False
    anti_example: bool = False  # code block introduced as "not supported" / "don't do this"


@dataclass
class DocPage:
    file: str
    lines: List[str]
    refs: List[DocRef]
    sections: List[Tuple[int, str]]  # (line, heading)

    def section_bounds(self, line: int) -> Tuple[int, int, str]:
        start, title = 1, ""
        for ln, t in self.sections:
            if ln <= line:
                start, title = ln, t
        nxt = [ln for ln, _ in self.sections if ln > line]
        end = (nxt[0] - 1) if nxt else len(self.lines)
        return start, end, title


def _strip_prompts(lines: List[str]) -> List[str]:
    if not any(l.lstrip().startswith(">>>") for l in lines):
        return lines
    out = []
    for l in lines:
        s = l.lstrip()
        out.append(s[4:] if s.startswith(">>> ") or s.startswith("... ") else (s[3:] if s in (">>>", "...") else ""))
    return out


def _dotted(node: ast.AST) -> Optional[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    if isinstance(node, ast.Call):  # httpx.Client().get(...)
        inner = _dotted(node.func)
        if inner:
            return inner + "()." + ".".join(reversed(parts)) if parts else inner + "()"
    return None


class _CodeVisitor(ast.NodeVisitor):
    def __init__(self, pkg: str, var_types: Dict[str, str], classes: Set[str]):
        self.pkg, self.vars, self.classes = pkg, var_types, classes
        self.hits: List[Tuple[int, str, Optional[str], List[Tuple[str, str]]]] = []

    def _resolve(self, dotted: str) -> Optional[str]:
        if dotted.startswith(self.pkg + "."):
            rest = dotted[len(self.pkg) + 1:]
            if "()." in rest:  # httpx.Client().get
                cls, meth = rest.split("().", 1)
                return f"{cls}.{meth}"
            return rest
        root, _, attr = dotted.partition(".")
        if root in self.vars and attr and "." not in attr:
            return f"{self.vars[root]}.{attr}"
        return None

    def _bind(self, target: ast.AST, value: ast.AST):
        if isinstance(target, ast.Name) and isinstance(value, ast.Call):
            d = _dotted(value.func)
            sym = self._resolve(d) if d else None
            if sym and sym.split(".")[0] in self.classes and "." not in sym:
                self.vars[target.id] = sym

    def visit_Assign(self, node):
        for t in node.targets:
            self._bind(t, node.value)
        self.generic_visit(node)

    def visit_With(self, node):
        for item in node.items:
            if item.optional_vars is not None:
                self._bind(item.optional_vars, item.context_expr)
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_Call(self, node):
        d = _dotted(node.func)
        if d:
            sym = self._resolve(d)
            if sym is not None:
                kws = [(k.arg, ast.unparse(k.value)) for k in node.keywords if k.arg]
                self.hits.append((node.lineno, d, sym, kws))
        self.generic_visit(node)


def _parse_block(code_lines: List[str]) -> List[ast.Module]:
    src = "\n".join(code_lines)
    try:
        return [ast.parse(src)]
    except SyntaxError:
        pass
    # fall back: parse line by line, padding so line numbers stay true
    mods = []
    for i, l in enumerate(code_lines):
        try:
            mods.append(ast.parse("\n" * i + l.strip()))
        except SyntaxError:
            continue
    return mods


def index_page(file: str, text: str, pkg: str, classes: Set[str], env_names: Set[str]) -> DocPage:
    lines = text.splitlines()
    refs: List[DocRef] = []
    sections: List[Tuple[int, str]] = []
    var_types: Dict[str, str] = {}
    section = ""
    i = 0
    env_rx = re.compile(r"\b(" + "|".join(sorted(map(re.escape, env_names), key=len, reverse=True)) + r")\b") if env_names else None
    sym_rx = re.compile(rf"\b{re.escape(pkg)}\.([A-Za-z_][\w.]*)")
    kw_rx = re.compile(r"`([a-z_][a-z0-9_]*)(?:=[^`]*)?`")
    while i < len(lines):
        line = lines[i]
        m = FENCE.match(line)
        if m:
            fence, lang = m.group(1), m.group(2).lower()
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(fence):
                j += 1
            body = lines[i + 1: j]
            start = i + 2  # 1-based line number of first body line
            prose_before = [l for l in lines[max(0, i - 4): i] if l.strip() and not FENCE.match(l)][-2:]
            anti = any(ANTI.search(re.sub(r"[*_`]", "", l)) for l in prose_before)
            if lang in PY_LANGS:
                v = _CodeVisitor(pkg, var_types, classes)
                for mod in _parse_block(_strip_prompts(body)):
                    v.visit(mod)
                seen = set()
                for ln, raw, sym, kws in v.hits:
                    key = (ln, raw)
                    if key in seen:
                        continue
                    seen.add(key)
                    refs.append(DocRef(file, start + ln - 1, "call", raw, sym, kws, section, True, anti))
            if env_rx:
                for k, b in enumerate(body):
                    for em in env_rx.finditer(b):
                        refs.append(DocRef(file, start + k, "env", em.group(1), section=section, in_code=True))
            i = j + 1
            continue
        h = HEADING.match(line)
        if h:
            section = h.group(2).strip()
            sections.append((i + 1, section))
        for sm in sym_rx.finditer(line):
            name = sm.group(1).rstrip(".")
            refs.append(DocRef(file, i + 1, "mention", f"{pkg}.{name}", name, section=section))
        for km in kw_rx.finditer(line):
            refs.append(DocRef(file, i + 1, "param_mention", km.group(1), section=section))
        if env_rx:
            for em in env_rx.finditer(line):
                refs.append(DocRef(file, i + 1, "env", em.group(1), section=section))
        i += 1
    return DocPage(file, lines, refs, sections)


def doc_files(snap: Snapshot, roots: Iterable[str] = ("docs/", "README.md")) -> List[str]:
    out = []
    for f in snap.files():
        if f.endswith(".md") and any(f == r or f.startswith(r) for r in roots):
            out.append(f)
    return sorted(out)


def index_docs(snap: Snapshot, pkg: str, classes: Set[str], env_names: Set[str],
               roots: Iterable[str] = ("docs/", "README.md")) -> Dict[str, DocPage]:
    return {f: index_page(f, snap.read(f), pkg, classes, env_names) for f in doc_files(snap, roots)}
