"""Evaluation harness.

E1  Historical replay on encode/httpx with hand-adjudicated ground truth (eval/ground_truth.json):
    span-level precision / recall for three operating points:
      static      deterministic rules only (what can block CI with no LLM)
      candidates  static + every review candidate, no judgement (upper bound on recall)
      agentic     after the LLM reasoner's verdicts (what a human reviewer actually sees)
    plus fix quality: fixes applied / blocked by guardrails / static errors remaining after the patch.
E2  Mutation benchmark: inject known API breaks into httpx 0.28.1 and check each is caught
    at every doc line that uses the mutated API (recall), and nothing else is flagged (false alarms).
E3  Cost and latency per run (LLM calls, $ and seconds), from the trace.
"""
from __future__ import annotations

import ast
import json
import random
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Tuple

from .apimodel import build_api
from .checks import static_checks
from .docindex import index_docs, index_page
from .pipeline import RunConfig, RunState, publish, run
from .snapshot import Snapshot

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples" / "httpx"


def _match(f, spans) -> int:
    for i, s in enumerate(spans):
        if f["file"] == s["file"] and s["lines"][0] <= f["line"] <= s["lines"][1]:
            return i
    return -1


def score(flags: List[Dict], gt: Dict) -> Dict:
    stale, neg = gt["stale"], gt["not_stale"]
    hit, fp, fp_neg = set(), 0, 0
    for f in flags:
        i = _match(f, stale)
        if i >= 0:
            hit.add(i)
        else:
            fp += 1
            fp_neg += _match(f, neg) >= 0
    tp = len(hit)
    flagged_spans = tp + fp
    return {"recall": tp / len(stale), "precision": tp / flagged_spans if flagged_spans else 1.0,
            "tp_spans": tp, "gt_spans": len(stale), "false_flags": fp, "false_flags_on_known_traps": fp_neg,
            "missed": [f"{s['file']}:{s['lines'][0]} ({s['source']})" for i, s in enumerate(stale) if i not in hit]}


def _dedupe_lines(fs: List[Dict]) -> List[Dict]:
    seen, out = set(), []
    for f in fs:
        k = (f["file"], f["line"])
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out


def replay(llm: str, out_root: Path) -> Dict:
    gt_all = json.loads((ROOT / "eval" / "ground_truth.json").read_text(encoding="utf-8"))
    results = {}
    for sc in gt_all["scenarios"]:
        t0 = time.perf_counter()
        st = RunState(RunConfig("httpx", llm=llm), Snapshot.from_dir(EXAMPLES / sc["base"], sc["base"]),
                      Snapshot.from_dir(EXAMPLES / sc["head"], sc["head"]), Snapshot.from_dir(EXAMPLES / sc["docs"], sc["docs"]))
        run(st)
        publish(st, out_root / sc["name"])
        F = [f.to_dict() for f in st.findings]
        static = _dedupe_lines([f for f in F if f["severity"] in ("error", "warning")])
        cands = _dedupe_lines(F)
        agentic = _dedupe_lines([f for f in F if f["verdict"] == "stale"])
        # fix quality: static findings left after applying the patch
        pages_after = dict(st.pages)
        for file, lines in st.patched.items():
            pages_after[file] = index_page(file, "\n".join(lines), "httpx", set(st.head_api.classes),
                                           set(st.head_api.env_reads) | set(st.base_api.env_reads))
        before = len(static_checks(st.pages, st.head_api, st.base_api))
        after = len(static_checks(pages_after, st.head_api, st.base_api))
        results[sc["name"]] = {
            "title": sc["title"],
            "static": score(static, sc), "candidates": score(cands, sc), "agentic": score(agentic, sc),
            "fixes": {"applied": sum((f.fix_status or "").startswith("applied") for f in st.findings),
                      "suggested": sum(f.fix_status == "suggestion" for f in st.findings),
                      "blocked_by_guardrails": sorted((f.fix_status or "") for f in st.findings
                                                     if (f.fix_status or "").startswith("rejected")),
                      "static_findings_before": before, "static_findings_after_patch": after},
            "llm": st.llm_stats, "seconds": round(time.perf_counter() - t0, 2),
        }
    return results


# ---------------------------------------------------------------------------
# E2 mutation benchmark
# ---------------------------------------------------------------------------
def _mutate(src_dir: Path, kind: str, target: Tuple[str, str, str]) -> None:
    """target = (file, qualname, param). Rewrites one function with ast (formatting is irrelevant)."""
    file, qual, param = target
    path = src_dir / file
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls, _, meth = qual.partition(".")
    for node in ast.walk(tree):
        if kind == "rename_symbol" and isinstance(node, ast.ClassDef) and node.name == cls and not meth:
            node.name = cls + "V2"
            break
        if isinstance(node, ast.ClassDef) and meth == "" and node.name == cls:
            fn = next((m for m in node.body if isinstance(m, ast.FunctionDef) and m.name == "__init__"), None)
        elif isinstance(node, ast.ClassDef) and node.name == cls:
            fn = next((m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == meth), None)
        elif isinstance(node, ast.FunctionDef) and not meth and node.name == cls and node in tree.body:
            fn = node
        else:
            continue
        if fn is None:
            continue
        if kind == "remove_param":
            a = fn.args
            for lst, dlist in ((a.args, a.defaults), (a.kwonlyargs, a.kw_defaults)):
                for i, arg in enumerate(lst):
                    if arg.arg == param:
                        if lst is a.kwonlyargs:
                            del a.kw_defaults[i]
                        else:
                            j = i - (len(a.args) - len(a.defaults))
                            if j >= 0:
                                del a.defaults[j]
                        del lst[i]
                        break
        elif kind == "deprecate_param":
            warn = ast.parse(f"if {param} is not None:\n    import warnings\n    warnings.warn('`{param}` is deprecated', "
                             f"DeprecationWarning)").body[0]
            fn.body.insert(0, warn)
        elif kind == "rename_symbol":
            fn.name = fn.name + "_v2"
        break
    path.write_text(ast.unparse(tree), encoding="utf-8")
    if kind == "rename_symbol":  # keep __all__ consistent with the rename, like a real refactor would
        init = src_dir / "httpx" / "__init__.py"
        init.write_text(init.read_text(encoding="utf-8").replace(f'"{cls}",', f'"{cls}V2",' if not meth and cls[0].isupper()
                                                                   else f'"{cls}_v2",'), encoding="utf-8")


def mutation_benchmark(n: int = 24, seed: int = 7) -> Dict:
    base_dir = EXAMPLES / "0.28.1"
    base_snap = Snapshot.from_dir(base_dir, "0.28.1")
    api = build_api(base_snap, "httpx")
    pages = index_docs(base_snap, "httpx", set(api.classes), set(api.env_reads))
    usage: Dict[Tuple[str, str], List[Tuple[str, int]]] = {}  # (qual, param) -> doc lines
    sym_usage: Dict[str, List[Tuple[str, int]]] = {}
    for p in pages.values():
        for r in p.refs:
            if r.kind in ("call", "mention") and r.symbol and not r.anti_example:
                root = r.symbol.split(".")[0]  # httpx.AsyncClient(...), httpx.AsyncClient in prose, ...
                if (r.file, r.line) not in sym_usage.get(root, []):
                    sym_usage.setdefault(root, []).append((r.file, r.line))
            if r.kind == "call" and r.symbol in api.callables and not r.anti_example:
                for k, _ in r.kwargs:
                    if k in api.callables[r.symbol].params:
                        usage.setdefault((r.symbol, k), []).append((r.file, r.line))
    rng = random.Random(seed)
    params = sorted(k for k in usage if k[1] not in api.callables[k[0]].deprecated)
    syms = sorted(s for s in sym_usage if "." not in s and s in api.public)
    cases = []
    for kind in ("remove_param", "deprecate_param"):
        for qual, param in rng.sample(params, min(n // 3, len(params))):
            cases.append((kind, qual, param, usage[(qual, param)]))
    for s in rng.sample(syms, min(n // 3, len(syms))):
        cases.append(("rename_symbol", s, "", sym_usage[s]))
    expected_rule = {"remove_param": "UNKNOWN_KWARG", "deprecate_param": "DEPRECATED_KWARG", "rename_symbol": "STALE_SYMBOL"}
    rows, tot_exp, tot_hit, tot_false = [], 0, 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        for kind, qual, param, lines in cases:
            work = Path(tmp) / "w"
            if work.exists():
                shutil.rmtree(work)
            shutil.copytree(base_dir, work)
            _mutate(work, kind, (api.callables[qual].file, qual, param))
            st = RunState(RunConfig("httpx", llm="off"), base_snap, Snapshot.from_dir(work, "mutant"), base_snap)
            run(st)
            new = [f for f in st.findings if f.rule == expected_rule[kind] and (param == "" or f.param == param)]
            got = {(f.file, f.line) for f in new}
            exp = set(lines)
            hit = len(exp & got)
            false = len(got - exp)
            tot_exp, tot_hit, tot_false = tot_exp + len(exp), tot_hit + hit, tot_false + false
            rows.append({"mutation": kind, "target": f"{qual}({param})" if param else qual, "doc_uses": len(exp),
                         "caught": hit, "false_alarms": false})
    return {"cases": rows, "recall": tot_hit / tot_exp if tot_exp else 0.0, "false_alarms": tot_false,
            "mutations": len(rows), "fully_caught": sum(r["caught"] == r["doc_uses"] for r in rows)}


def render(rep: Dict, mut: Dict) -> str:
    L = ["# DocSync evaluation (auto-generated)\n", "## E1 Historical replay on encode/httpx\n"]
    for name, r in rep.items():
        L += [f"### {name}: {r['title']}\n", "| operating point | recall | precision | false flags (on known traps) | missed |",
              "|---|---|---|---|---|"]
        for op in ("static", "candidates", "agentic"):
            s = r[op]
            L.append(f"| {op} | {s['tp_spans']}/{s['gt_spans']} = {s['recall']:.2f} | {s['precision']:.2f} | "
                     f"{s['false_flags']} ({s['false_flags_on_known_traps']}) | {'; '.join(s['missed']) or '-'} |")
        fx = r["fixes"]
        L.append(f"\nFixes applied: {fx['applied']} · suggested only: {fx['suggested']} · blocked by guardrails: "
                 f"{len(fx['blocked_by_guardrails'])} {fx['blocked_by_guardrails'] or ''} · static findings "
                 f"{fx['static_findings_before']} → {fx['static_findings_after_patch']} after patch · LLM "
                 f"{r['llm'].get('mode')} ({r['llm'].get('live_calls', 0)} live, {r['llm'].get('replayed', 0)} replayed, "
                 f"${r['llm'].get('cost_usd', 0):.3f}) · {r['seconds']}s\n")
    L += ["## E2 Mutation benchmark (httpx 0.28.1, LLM off)\n",
          f"{mut['mutations']} injected API breaks · doc-usage recall **{mut['recall']:.2f}** · fully caught "
          f"{mut['fully_caught']}/{mut['mutations']} · false alarms {mut['false_alarms']}\n",
          "| mutation | target | doc lines using it | caught | false alarms |", "|---|---|---|---|---|"]
    L += [f"| {c['mutation']} | `{c['target']}` | {c['doc_uses']} | {c['caught']} | {c['false_alarms']} |" for c in mut["cases"]]
    return "\n".join(L) + "\n"


def main(llm: str = "replay", out: Path = None) -> Dict:
    out = out or ROOT / "out" / "eval"
    rep = replay(llm, out)
    mut = mutation_benchmark()
    md = render(rep, mut)
    (ROOT / "eval" / "results.md").write_text(md, encoding="utf-8")
    (ROOT / "eval" / "results.json").write_text(json.dumps({"replay": rep, "mutation": mut}, indent=1), encoding="utf-8")
    print(md)
    return {"replay": rep, "mutation": mut}
