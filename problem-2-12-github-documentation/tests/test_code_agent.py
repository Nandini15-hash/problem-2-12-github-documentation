from src.docsync.apidiff import diff_api


def test_public_api_and_env_reads(apis):
    a = apis["0.28.1"]
    assert {"Client", "AsyncClient", "get", "Timeout"} <= a.public
    assert "timeout" in a.callables["Client"].params
    assert set(a.env_reads) >= {"SSL_CERT_FILE", "SSL_CERT_DIR"}
    assert "SSLKEYLOGFILE" in apis["0.27.2"].env_reads and "SSLKEYLOGFILE" not in a.env_reads


def test_deprecation_found_through_keyword_forwarding(apis):
    c = apis["0.28.0"].callables["Client"]
    # Client(cert=...) only warns inside create_ssl_context(); we must follow the forwarding chain
    assert c.deprecated["cert"] == "cert"
    assert c.deprecated["verify"] == "isinstance(verify, str)"
    assert any("load_cert_chain" in m for m in apis["0.28.0"].deprecation_messages["create_ssl_context"])


def test_properties_are_not_callables(apis):
    assert "Client.timeout" not in apis["0.28.1"].callables


def test_diff_finds_the_real_0_28_breaking_changes(apis):
    ch = {(c.kind, c.symbol, c.param) for c in diff_api(apis["0.27.2"], apis["0.28.0"])}
    assert ("param_removed", "Client", "proxies") in ch
    assert ("param_removed", "Client", "app") in ch
    assert ("param_deprecated", "Client", "cert") in ch
    assert ("param_removed", "create_ssl_context", "http2") in ch
    assert ("env_removed", "SSLKEYLOGFILE", None) in ch
    assert diff_api(apis["0.28.1"], apis["0.28.1"]) == []
