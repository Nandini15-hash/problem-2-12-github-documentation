from src.docsync.checks import Finding
from src.docsync.guardrails import GuardConfig, verify_fixes
from src.docsync.docindex import index_page

MD = """# SSL

Use a custom CA bundle:

```python
r = httpx.get("https://example.org", verify="ca.pem")
```
See https://example.org/docs.
"""


def _run(apis, fix, file="docs/ssl.md", conf=0.9):
    a = apis["0.28.1"]
    page = index_page(file, MD, "httpx", set(a.classes), set())
    f = Finding(file, 6, "DEPRECATED_KWARG", "m", symbol="get", param="verify", confidence=conf)
    f.verdict, f.fix = "stale", fix
    patched = verify_fixes([f], {file: page}, a, "httpx", set(a.classes), set(), GuardConfig())
    return f.fix_status, patched


GOOD = "import ssl\nctx = ssl.create_default_context(cafile=\"ca.pem\")\nr = httpx.get(\"https://example.org\", verify=ctx)"


def test_good_fix_is_applied(apis):
    status, patched = _run(apis, {"start_line": 6, "end_line": 6, "replacement": GOOD})
    assert status == "applied" and "verify=ctx" in "\n".join(patched["docs/ssl.md"])


def test_each_guardrail_blocks(apis):
    cases = {
        "G1_path": dict(fix={"start_line": 6, "end_line": 6, "replacement": GOOD}, file="src/httpx/_api.md"),
        "G2_scope": dict(fix={"start_line": 1, "end_line": 200, "replacement": GOOD}),
        "G4_new_link": dict(fix={"start_line": 6, "end_line": 6, "replacement": GOOD + "\n# https://evil.example"}),
        "G5_secret": dict(fix={"start_line": 6, "end_line": 6, "replacement": GOOD + "  # ghp_" + "a" * 30}),
        "G6_syntax": dict(fix={"start_line": 6, "end_line": 6, "replacement": "r = httpx.get(("}),
        "G7_introduces_api_error": dict(fix={"start_line": 6, "end_line": 6, "replacement": "r = httpx.get('u', nope=1)"}),
        "G7_not_resolved": dict(fix={"start_line": 6, "end_line": 6, "replacement": "r = httpx.get('u', verify='x.pem')"}),
    }
    for want, kw in cases.items():
        status, patched = _run(apis, **kw)
        assert status == f"rejected:{want}", (want, status)
        assert not patched


def test_low_confidence_becomes_suggestion(apis):
    status, patched = _run(apis, {"start_line": 6, "end_line": 6, "replacement": GOOD}, conf=0.4)
    assert status == "suggestion" and not patched
