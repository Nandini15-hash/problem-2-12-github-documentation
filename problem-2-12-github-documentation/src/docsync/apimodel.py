"""Code agent, part 1: extract the public API of a Python package with `ast`.

No imports and no execution, so it is safe on untrusted code and works on any
git ref. For each public symbol it records parameters, defaults, docstrings and
*deprecated* parameters, which it finds by spotting `warnings.warn(...,
DeprecationWarning)` guarded by an `if` that tests the parameter and then
propagating that through keyword forwarding (`Client(cert=...)` ->
`create_ssl_context(cert=cert)`). It also records which environment
variables the code reads.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .snapshot import Snapshot


@dataclass
class Param:
    name: str
    kind: str  # pos | kwonly | varargs | varkw
    default: Optional[str] = None

    @property
    def required(self) -> bool:
        return self.default is None and self.kind in ("pos", "kwonly")


@dataclass
class Callable_:
    qualname: str  # e.g. "Client.get" or "get"
    params: Dict[str, Param] = field(default_factory=dict)
    deprecated: Dict[str, str] = field(default_factory=dict)  # param -> condition under which it warns
    doc: str = ""
    file: str = ""
    line: int = 0

    @property
    def has_varkw(self) -> bool:
        return any(p.kind == "varkw" for p in self.params.values())

    def signature(self) -> str:
        parts = []
        for p in self.params.values():
            s = {"varargs": "*", "varkw": "**"}.get(p.kind, "") + p.name
            if p.default is not None:
                s += f"={p.default}"
            if p.name in self.deprecated:
                s += f"  # deprecated when: {self.deprecated[p.name]}"
            parts.append(s)
        return f"{self.qualname}({', '.join(parts)})"


@dataclass
class ApiModel:
    package: str
    label: str
    callables: Dict[str, Callable_] = field(default_factory=dict)  # qualname -> callable
    classes: Dict[str, List[str]] = field(default_factory=dict)  # class -> public method names
    public: Set[str] = field(default_factory=set)
    env_reads: Dict[str, List[Tuple[str, int, str]]] = field(default_factory=dict)  # VAR -> [(file, line, code)]
    deprecation_messages: Dict[str, List[str]] = field(default_factory=dict)  # qualname -> warning texts

    def get(self, qualname: str) -> Optional[Callable_]:
        return self.callables.get(qualname)


# ---------------------------------------------------------------------------
def _params(fn: ast.FunctionDef, drop_self: bool) -> Dict[str, Param]:
    a = fn.args
    out: Dict[str, Param] = {}
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + [ast.unparse(d) for d in a.defaults]
    for i, (arg, d) in enumerate(zip(pos, defaults)):
        if drop_self and i == 0 and arg.arg in ("self", "cls"):
            continue
        out[arg.arg] = Param(arg.arg, "pos", d)
    if a.vararg:
        out[a.vararg.arg] = Param(a.vararg.arg, "varargs")
    for arg, d in zip(a.kwonlyargs, a.kw_defaults):
        out[arg.arg] = Param(arg.arg, "kwonly", ast.unparse(d) if d is not None else None)
    if a.kwarg:
        out[a.kwarg.arg] = Param(a.kwarg.arg, "varkw")
    return out


def _names_in(node: ast.AST) -> Set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _directly_deprecated(fn: ast.FunctionDef, params: Set[str]) -> Dict[str, str]:
    """Params tested by an `if` whose body emits a DeprecationWarning -> the `if` condition."""
    dep: Dict[str, str] = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        warns = [c for c in ast.walk(ast.Module(body=node.body, type_ignores=[])) if isinstance(c, ast.Call)
                 and ast.unparse(c.func).endswith("warn") and "DeprecationWarning" in ast.unparse(c)]
        if warns:
            for name in _names_in(node.test) & params:
                dep.setdefault(name, ast.unparse(node.test))
    return dep


def _warn_messages(fn: ast.FunctionDef) -> List[str]:
    """Literal text of DeprecationWarning messages (they usually name the replacement)."""
    msgs: List[str] = []
    assigns: List[Tuple[int, str, str]] = []  # (line, var, text) - same var may be reassigned
    for n in ast.walk(fn):  # message = "..." ; warnings.warn(message, DeprecationWarning)
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            parts = [c.value for c in ast.walk(n.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            if parts:
                assigns.append((n.lineno, n.targets[0].id, "".join(parts)))
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and ast.unparse(c.func).endswith("warn") and "DeprecationWarning" in ast.unparse(c):
            if c.args:
                a = c.args[0]
                if isinstance(a, ast.Name):
                    prior = [t for ln, v, t in sorted(assigns) if v == a.id and ln <= c.lineno]
                    if prior:
                        msgs.append(prior[-1])
                else:
                    parts = [x.value for x in ast.walk(a) if isinstance(x, ast.Constant) and isinstance(x.value, str)]
                    if parts:
                        msgs.append("".join(parts))
    msgs = list(dict.fromkeys(msgs))
    return [" ".join(m.split())[:300] for m in msgs]


def _forwards(fn: ast.FunctionDef) -> List[Tuple[str, bool, str, str]]:
    """(callee_name, is_method_call, callee_kw, our_param) for calls like callee(x=x)."""
    out = []
    for c in ast.walk(fn):
        if isinstance(c, ast.Call) and isinstance(c.func, (ast.Name, ast.Attribute)):
            is_attr = isinstance(c.func, ast.Attribute)
            callee = c.func.attr if is_attr else c.func.id
            for kw in c.keywords:
                if kw.arg and isinstance(kw.value, ast.Name):
                    out.append((callee, is_attr, kw.arg, kw.value.id))
    return out


def _env_reads(tree: ast.AST, src: str, path: str, sink: Dict[str, List[Tuple[str, int, str]]]):
    lines = src.splitlines()
    for n in ast.walk(tree):
        key = None
        if isinstance(n, ast.Call) and ast.unparse(n.func) in ("os.environ.get", "os.getenv", "environ.get", "getenv"):
            if n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
                key = n.args[0].value
        elif isinstance(n, ast.Subscript) and ast.unparse(n.value) in ("os.environ", "environ"):
            if isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                key = n.slice.value
        if key:
            sink.setdefault(key, []).append((path, n.lineno, lines[n.lineno - 1].strip()))


def build_api(snap: Snapshot, package: str) -> ApiModel:
    api = ApiModel(package=package, label=snap.label)
    funcs: Dict[str, Tuple[ast.FunctionDef, str, bool]] = {}  # qualname -> (node, file, is_method)
    class_nodes: Dict[str, Tuple[ast.ClassDef, str]] = {}
    for path in snap.glob(f"{package}/", ".py"):
        src = snap.read(path)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        _env_reads(tree, src, path, api.env_reads)
        if path == f"{package}/__init__.py":
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__all__" for t in node.targets):
                    api.public |= {e.value for e in getattr(node.value, "elts", []) if isinstance(e, ast.Constant)}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs.setdefault(node.name, (node, path, False))
            elif isinstance(node, ast.ClassDef):
                class_nodes.setdefault(node.name, (node, path))

    def class_methods(name: str, seen=()) -> Dict[str, Tuple[ast.FunctionDef, str]]:
        if name not in class_nodes or name in seen:
            return {}
        node, path = class_nodes[name]
        out: Dict[str, Tuple[ast.FunctionDef, str]] = {}
        for b in node.bases:  # inherited first, then overridden
            out.update(class_methods(ast.unparse(b).split(".")[-1], seen + (name,)))
        for m in node.body:
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[m.name] = (m, path)
        return out

    # every function/method (private ones too: deprecation often flows through helpers)
    allf: Dict[str, Tuple[ast.FunctionDef, str, bool, str]] = {
        q: (n, p, False, ast.get_docstring(n) or "") for q, (n, p, _m) in funcs.items()}
    for cname, (cnode, cpath) in class_nodes.items():
        methods = class_methods(cname)
        api.classes[cname] = sorted(m for m in methods if not m.startswith("_"))
        cls_doc = ast.get_docstring(cnode) or ""
        for mname, (m, path) in methods.items():
            decos = {ast.unparse(d) for d in m.decorator_list}
            if any(d == "property" or d.endswith((".setter", ".getter", ".deleter")) for d in decos):
                continue  # attributes, not callables
            q = cname if mname == "__init__" else f"{cname}.{mname}"
            allf[q] = (m, path, True, (ast.get_docstring(m) or cls_doc) if mname == "__init__" else (ast.get_docstring(m) or ""))
        if "__init__" not in methods:  # class without a constructor of its own
            empty = ast.parse("def __init__(self): pass").body[0]
            empty.lineno = cnode.lineno
            allf[cname] = (empty, cpath, True, cls_doc)

    every: Dict[str, Callable_] = {}
    msgs: Dict[str, List[str]] = {}
    for q, (node, path, is_method, doc) in allf.items():
        m = _warn_messages(node)
        if m:
            msgs[q] = m
        params = _params(node, drop_self=is_method)
        every[q] = Callable_(q, params, _directly_deprecated(node, set(params)), doc.strip(), path,
                             getattr(node, "lineno", 0))

    # propagate deprecation through keyword forwarding (incl. private helpers), to a fixpoint
    funcs_by_name: Dict[str, List[str]] = {}  # bare-name calls: module functions and class constructors
    meths_by_name: Dict[str, List[str]] = {}  # attribute calls: methods
    for q in every:
        if "." in q:
            meths_by_name.setdefault(q.split(".")[-1], []).append(q)
        else:
            funcs_by_name.setdefault(q, []).append(q)
    changed = True
    while changed:
        changed = False
        for q, (node, *_rest) in allf.items():
            me = every[q]
            for callee, is_attr, kw, ours in _forwards(node):
                if ours in me.params and ours not in me.deprecated:
                    for t in (meths_by_name if is_attr else funcs_by_name).get(callee, []):
                        if kw in every[t].deprecated:
                            cond = every[t].deprecated[kw]
                            me.deprecated[ours] = re.sub(rf"\b{re.escape(kw)}\b", ours, cond)
                            msgs[q] = list(dict.fromkeys(msgs.get(q, []) + msgs.get(t, [])))
                            changed = True
                            break

    # keep only the public surface: public funcs/classes and their public methods
    for q, c in every.items():
        head, _, tail = q.partition(".")
        if head.startswith("_") or (tail and tail.startswith("_")):
            continue
        api.callables[q] = c
        if q in msgs and c.deprecated:
            api.deprecation_messages[q] = msgs[q]

    if not api.public:
        api.public = {q for q in api.callables if "." not in q and not q.startswith("_")}
    return api
