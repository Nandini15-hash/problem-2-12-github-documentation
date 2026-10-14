from src.docsync.checks import deprecation_applies, static_checks
from src.docsync.docindex import index_page

MD = '''# Title

```pycon
>>> client = httpx.Client(timeout=5)
>>> r = client.get("https://x", extensions={})
```

This usage is **not** supported:

```python
client = httpx.Client()
client.post("u", cookies={"a": "b"})
```

```python
async with httpx.AsyncClient(bogus=1) as c:
    await c.get("u")
```

Set `SSLKEYLOGFILE` to debug. See `httpx.get` and `proxies=...`.
'''


def _page(apis):
    a = apis["0.28.1"]
    return index_page("docs/x.md", MD, "httpx", set(a.classes), {"SSLKEYLOGFILE"})


def test_index_resolves_variables_prompts_and_kinds(apis):
    p = _page(apis)
    calls = {(r.line, r.symbol) for r in p.refs if r.kind == "call"}
    assert (4, "Client") in calls and (5, "Client.get") in calls  # >>> prompts stripped, var bound
    assert (16, "AsyncClient") in calls and (17, "AsyncClient.get") in calls  # async with binding
    kinds = {r.kind for r in p.refs if r.line == 20}
    assert kinds == {"env", "mention", "param_mention"}
    assert all(r.anti_example for r in p.refs if r.line in (11, 12) and r.kind == "call")


def test_static_rules(apis):
    f = static_checks({"docs/x.md": _page(apis)}, apis["0.28.1"])
    got = {(x.line, x.rule, x.param) for x in f}
    assert (16, "UNKNOWN_KWARG", "bogus") in got
    assert not any(x.line == 12 for x in f)  # anti-example is not a finding
    assert not any(x.line == 5 for x in f)  # Client.get(extensions=) is valid


def test_deprecation_condition_matching_never_evals():
    assert deprecation_applies("isinstance(verify, str)", "verify", "'ca.pem'") is True
    assert deprecation_applies("isinstance(verify, str)", "verify", "False") is False
    assert deprecation_applies("isinstance(verify, str)", "verify", "ctx") is False  # can't prove -> stay quiet
    assert deprecation_applies("cert", "cert", "cert") is True
    assert deprecation_applies("cookies is not None", "cookies", "None") is False
    assert deprecation_applies("__import__('os').system('x')", "x", "1") is None
