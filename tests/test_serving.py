import hashlib
import hmac
import json

from src.docsync.concierge import Concierge
from src.docsync.connectors import verify_github_signature
from src.docsync.docindex import index_docs
from src.docsync.mcp_server import handle


def _c(snaps, apis):
    a = apis["0.28.1"]
    return Concierge(a, index_docs(snaps["0.28.1"], "httpx", set(a.classes), set(a.env_reads)))


def test_search_and_symbol_cards(snaps, apis):
    c = _c(snaps, apis)
    assert c.search("how do I set a timeout")[0].symbol == "Timeout"
    card = c.get("httpx.Client")
    assert "cert" in " ".join(card.deprecations) and card.examples
    assert not any("extensions.md:169" in e for e in c.get("get").examples)  # broken doc example is not served


def test_check_snippet_gives_migration_hint(snaps, apis):
    issues = _c(snaps, apis).check_snippet('import httpx\nhttpx.Client(verify="ca.pem")\n')
    assert issues and "create_default_context" in issues[0]["hint"]


def test_mcp_protocol(snaps, apis):
    c = _c(snaps, apis)
    assert handle(c, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]["serverInfo"]["name"] == "docsync"
    tools = handle(c, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {"search_api", "get_symbol", "check_snippet"}
    r = handle(c, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "check_snippet", "arguments": {"code": "httpx.get('u', extensions={})"}}})
    assert "UNKNOWN_KWARG" in r["result"]["content"][0]["text"]
    assert "error" in handle(c, {"jsonrpc": "2.0", "id": 4, "method": "nope"})


def test_webhook_signature():
    body = json.dumps({"a": 1}).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert verify_github_signature("s3cret", body, sig)
    assert not verify_github_signature("s3cret", body + b" ", sig)
    assert not verify_github_signature("", body, sig)
