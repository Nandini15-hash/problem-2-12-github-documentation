import json
import os
import stat

import pytest

from src.docsync.reasoner import SYSTEM, Reasoner, _extract_json


def test_extract_json_tolerates_fences_and_chatter():
    assert _extract_json('Sure!\n```json\n{"flags": []}\n```') == {"flags": []}
    assert _extract_json("no json here") is None


@pytest.mark.skipif(os.name == "nt", reason="fake `claude` shim is a POSIX script")
def test_live_mode_invokes_claude_without_tools_and_passes_key(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    log = tmp_path / "argv.json"
    fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                    f"json.dump({{'argv': sys.argv[1:], 'tok': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'), "
                    f"'stdin': sys.stdin.read()}}, open({str(log)!r}, 'w'))\n"
                    "print(json.dumps({'result': '{\"flags\": [{\"id\": \"x\", \"verdict\": \"ok\"}]}', "
                    "'total_cost_usd': 0.01}))\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_KEY", "test-key")
    monkeypatch.delenv("DOCSYNC_LLM", raising=False)
    r = Reasoner("auto", cassette_dir=tmp_path / "cas")
    assert r.mode == "live"
    assert r.ask("PROMPT") == {"flags": [{"id": "x", "verdict": "ok"}]}
    call = json.loads(log.read_text())
    assert call["argv"][call["argv"].index("--tools") + 1] == "" and "-p" in call["argv"]
    assert call["tok"] == "test-key" and call["stdin"] == "PROMPT"
    assert SYSTEM in call["argv"]
    # recorded -> a replay run gives the same answer with no process spawned
    r2 = Reasoner("replay", cassette_dir=tmp_path / "cas")
    assert r2.ask("PROMPT") == {"flags": [{"id": "x", "verdict": "ok"}]} and r2.stats["replayed"] == 1
