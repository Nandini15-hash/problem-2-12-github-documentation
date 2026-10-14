import json

from src.docsync.pipeline import RunConfig, RunState, publish, run, exit_code


def test_replay_scenario_matches_committed_cassettes(snaps, tmp_path):
    st = run(RunState(RunConfig("httpx", llm="replay"), snaps["0.27.2"], snaps["0.28.0"], snaps["0.27.2"]))
    assert st.llm_stats["missing_cassette"] == 0
    stale = {(f.file, f.line) for f in st.findings if f.verdict == "stale"}
    assert ("docs/advanced/extensions.md", 169) in stale
    assert ("docs/environment_variables.md", 11) in stale
    ok = {(f.file, f.line) for f in st.findings if f.verdict == "ok"}
    assert ("docs/compatibility.md", 166) in ok  # talks about `requests`, not httpx
    paths = publish(st, tmp_path)
    assert "verify=ctx" in paths["patch"].read_text() or "create_default_context" in paths["patch"].read_text()
    assert json.loads(paths["findings"].read_text())
    assert exit_code(st) == 1  # CI fails while broken examples remain


def test_llm_off_still_catches_static_breakage(snaps):
    st = run(RunState(RunConfig("httpx", llm="off"), snaps["0.28.0"], snaps["0.28.1"], snaps["0.28.1"]))
    assert {("docs/advanced/extensions.md", 169), ("docs/advanced/extensions.md", 179)} <= {
        (f.file, f.line) for f in st.findings if f.verdict == "stale"}
