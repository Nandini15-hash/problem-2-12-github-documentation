# DocSync evaluation (auto-generated)

## E1 Historical replay on encode/httpx

### replay-0.28.0: Code moved 0.27.2 -> 0.28.0, docs left at 0.27.2 (simulated 'forgot the docs' PR)

| operating point | recall | precision | false flags (on known traps) | missed |
|---|---|---|---|---|
| static | 8/10 = 0.80 | 1.00 | 0 (0) | docs/compatibility.md:173 (human); docs/compatibility.md:201 (exec) |
| candidates | 10/10 = 1.00 | 0.83 | 2 (2) | - |
| agentic | 10/10 = 1.00 | 0.91 | 1 (1) | - |

Fixes applied: 17 · suggested only: 2 · blocked by guardrails: 0  · static findings 12 → 0 after patch · LLM replay (0 live, 9 replayed, $0.000) · 0.64s

### audit-0.28.1: Released docs vs released code, httpx 0.28.1

| operating point | recall | precision | false flags (on known traps) | missed |
|---|---|---|---|---|
| static | 2/4 = 0.50 | 1.00 | 0 (0) | docs/advanced/ssl.md:72 (later_fix); docs/compatibility.md:199 (exec) |
| candidates | 3/4 = 0.75 | 1.00 | 0 (0) | docs/compatibility.md:199 (exec) |
| agentic | 3/4 = 0.75 | 1.00 | 0 (0) | docs/compatibility.md:199 (exec) |

Fixes applied: 4 · suggested only: 0 · blocked by guardrails: 0  · static findings 2 → 0 after patch · LLM replay (0 live, 2 replayed, $0.000) · 0.74s

## E2 Mutation benchmark (httpx 0.28.1, LLM off)

24 injected API breaks · doc-usage recall **1.00** · fully caught 24/24 · false alarms 0

| mutation | target | doc lines using it | caught | false alarms |
|---|---|---|---|---|
| remove_param | `Client(timeout)` | 3 | 3 | 0 |
| remove_param | `Client(auth)` | 7 | 7 | 0 |
| remove_param | `Client.get(params)` | 1 | 1 | 0 |
| remove_param | `Timeout(connect)` | 1 | 1 | 0 |
| remove_param | `AsyncClient(http2)` | 3 | 3 | 0 |
| remove_param | `AsyncClient(transport)` | 3 | 3 | 0 |
| remove_param | `HTTPTransport(uds)` | 1 | 1 | 0 |
| remove_param | `AsyncHTTPTransport(retries)` | 1 | 1 | 0 |
| deprecate_param | `Client.get(extensions)` | 3 | 3 | 0 |
| deprecate_param | `NetRCAuth(file)` | 2 | 2 | 0 |
| deprecate_param | `AsyncClient(http2)` | 3 | 3 | 0 |
| deprecate_param | `HTTPTransport(proxy)` | 18 | 18 | 0 |
| deprecate_param | `Client(event_hooks)` | 3 | 3 | 0 |
| deprecate_param | `AsyncClient(base_url)` | 2 | 2 | 0 |
| deprecate_param | `AsyncClient.send(stream)` | 1 | 1 | 0 |
| deprecate_param | `Cookies.set(domain)` | 2 | 2 | 0 |
| rename_symbol | `ProtocolError` | 1 | 1 | 0 |
| rename_symbol | `Auth` | 2 | 2 | 0 |
| rename_symbol | `DigestAuth` | 3 | 3 | 0 |
| rename_symbol | `BaseTransport` | 1 | 1 | 0 |
| rename_symbol | `Response` | 4 | 4 | 0 |
| rename_symbol | `Proxy` | 3 | 3 | 0 |
| rename_symbol | `AsyncHTTPTransport` | 2 | 2 | 0 |
| rename_symbol | `ResponseNotRead` | 1 | 1 | 0 |
