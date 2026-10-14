# Problem 2.12: GitHub Documentation (DocSync)

DocSync flags documentation that the code has outgrown, proposes fixes that pass hard guardrails, and serves docs generated from the code so developers never read a stale page.

Tried on **encode/httpx**, it found two doc errors that are **still live on httpx `master`**, both proven by running the documented code (`TypeError`). It also caught one that shipped in 0.28.0 and took 9½ months to fix upstream. See **[docs/WRITEUP.md](docs/WRITEUP.md)** (2 pages) and **[eval/results.md](eval/results.md)**.

## Run it

Everything runs offline and needs Python ≥ 3.9 and git; there are no runtime dependencies. The exact grader commands are in [`run.yaml`](run.yaml).

```bash
pip install -r requirements.txt                       # just pytest
python -m src.docsync.cli demo --llm replay            # the workflow on 2 real httpx scenarios -> out/demo/
python -m src.docsync.cli eval --llm replay            # ground-truth replay + mutation benchmark -> eval/results.md
python -m src.docsync.cli ask how do I use a client certificate
python -m src.docsync.cli check examples/snippet_with_stale_api.py --head-dir examples/httpx/0.28.1
python -m pytest -q
```

**On your own repo**, with a live LLM (the `claude` CLI installed and `CLAUDE_KEY` set):

```bash
python -m src.docsync.cli scan --repo /path/to/repo --base main --head HEAD --package mypkg --llm live --out out/
#   --open-pr [--yes]          commit the guarded patch to a docsync/* branch and open a PR (dry-run without --yes)
#   --notify-webhook URL       POST a summary (Slack/Teams-compatible JSON)
#   --fail-on error|warning|never   CI exit code policy
python -m src.docsync.cli serve --repo /path/to/repo --port 8787    # GitHub webhook (HMAC) + POST /scan
claude mcp add docsync -- python -m src.docsync.cli mcp --head-dir /path/to/repo --package mypkg
```

## What you get from a run

| file | for whom |
|---|---|
| `report.md` | humans: what is stale, why, what was fixed, what was dismissed as still correct |
| `docs.patch` / `patched/` | the guardrail-approved edits |
| `findings.json` | machines: every flag with rule, verdict, confidence, reason and fix status |
| `trace.jsonl` | ops: seconds per agent, LLM calls, cost |

## How it works

| agent | does | LLM? |
|---|---|---|
| Code agent (`apimodel.py`, `apidiff.py`) | public API, deprecations (followed through keyword forwarding), env vars, typed API diff | no |
| Docs agent (`docindex.py`, `checks.py`) | indexes every call / mention / env var in Markdown; precise rules + review candidates | no |
| Reasoning agent (`reasoner.py`) | `claude -p` with **no tools**: stale/ok verdict + minimal fix, as JSON | yes |
| Verifier (`guardrails.py`) | G1–G9: doc paths only, in-section, size, no new links, no secrets, no new syntax errors, re-check passes, confidence, no overlap | no |
| Publisher (`pipeline.py`, `connectors.py`) | report, patch, PR on a new branch, PR comment, webhooks | no |
| Concierge (`concierge.py`, `mcp_server.py`) | search / get_symbol / check_snippet from live code, over CLI and MCP | no |

The diagram is in [docs/architecture.drawio](docs/architecture.drawio), and there is a mermaid version in the write-up.

## LLM modes

`--llm live` calls `claude -p` and records each answer to `cassettes/`. `replay` answers from cassettes (deterministic, free; what the graders run). `auto` uses live if `CLAUDE_KEY` is set, otherwise replay. `off` runs the deterministic rules only, which have precision 1.0 on our ground truth.

## Layout

```
src/docsync/      agents, pipeline, connectors, CLI, MCP server, evaluation
examples/httpx/   vendored httpx 0.27.2 / 0.28.0 / 0.28.1 (BSD-3, see SOURCE.md)
cassettes/        recorded claude -p answers (prompt + response, auditable)
eval/             ground_truth.json, results.md / results.json
deploy/           github-action.yml, Dockerfile
tests/            18 tests: code/docs agents, every guardrail, claude -p invocation contract, e2e, MCP, HMAC
```
