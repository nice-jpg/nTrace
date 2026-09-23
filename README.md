# nTrace

Local-first smart trace for the Bines LangChain agents. It records host and LLM spans,
persists them in SQLite, streams new events over WebSocket, and renders a multi-agent
timeline in React.

## Run

Install the Python service dependencies:

```bash
python -m pip install -r nTrace/requirements.txt
```

Build the frontend once, then start the combined server:

```bash
cd nTrace/frontend
npm install
npm run build
cd ../..
python -m nTrace.server
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The service binds only to
localhost by default. Bines conductor and collector agents emit traces automatically;
telemetry failures never fail an agent invocation.

For frontend development, run `npm run dev` in `nTrace/frontend`. Vite proxies both
HTTP and WebSocket traffic to the Python server.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `NTRACE_ENABLED` | `1` | Set to `0`, `false`, `no`, or `off` to disable emission. |
| `NTRACE_SERVER_URL` | `http://127.0.0.1:8765` | SDK event receiver. |
| `NTRACE_HOST` | `127.0.0.1` | Server bind address. |
| `NTRACE_PORT` | `8765` | Server port. |
| `NTRACE_DATABASE_PATH` | `nTrace/data/ntrace.sqlite3` | SQLite database path. |

## Agent integration

Trace construction is internal to each Bines agent.
Callers create and run `AgentRuntime` normally; no trace object or trace identifier is
passed through public constructors. Before each `run_turn`, the agent replaces its sole
active trace, records the current `session_id`, and marks it as a user-input root boundary.
The server never folds that boundary into an older active trace. `resume_turn` keeps the
active trace and does not create a new user-input boundary.
Each dynamic collector owns an independent trace and does not inherit client execution
context. Collectors do not carry the user-input boundary; the server infers their
parent/child relationships from host-span timing and recent tool calls, then persists the
resulting trace tree.

For every model iteration, the first trace middleware emits a host start from
`before_model`, the last trace middleware collects the prepared state and emits the
matching host end from `before_model`, and its `wrap_model_call` emits the LLM start/end
pair. The UI timeline advances only when an event arrives, so idle wall-clock time does
not continuously resize existing blocks; the next event reveals the elapsed gap.

The low-level `NTrace`, `createNTraceStartMiddleware`, and `createNTraceEndMiddleware`
exports are intended for agent-builder implementations, not for call sites invoking an
already-built agent. The compiled agent itself remains unwrapped and privately owns its
single active `NTrace` instance.

The event receiver accepts a single event or `{ "events": [...] }` at
`POST /api/v1/events`. History is available from `GET /api/v1/traces`; the lightweight
timeline uses `GET /api/v1/traces/{trace_id}/timeline`. Non-input span details load from
`GET /api/v1/traces/{trace_id}/spans/{span_id}/details`, while user inputs are paged
newest-first through `GET /api/v1/traces/{trace_id}/spans/{span_id}/user-inputs`.
Live updates use `WS /api/v1/stream`.

## Large contexts and reverse proxies

The SDK compresses event packets of at least 64 KiB with gzip, splits batches above
512 KiB on the wire (or 8 MiB before compression), and splits multi-event batches
again if the proxy returns HTTP 413. Single events remain intact: context fields are
never truncated to fit a request. The receiver accepts both ordinary JSON and gzip
JSON, validates the same event schema, and limits both transmitted and decoded
bodies to 64 MiB. Upgrade/restart the receiver **before** upgrading/restarting Agent
processes: older receivers cannot decode compressed requests. No frontend rebuild or
database migration is required.

Nginx defaults to a 1 MiB request-body limit. Large host/start contexts can exceed
this limit while smaller LLM/end packets still succeed, leaving missing/open host
spans and end-only LLM spans displayed with zero duration. Refreshing cannot recover
events that never reached storage. For large individual events, configure the
existing API proxy location, for example:

```nginx
location /api/ {
    client_max_body_size 64m;
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

Validate with `sudo nginx -t`, then reload with `sudo systemctl reload nginx`.
Keep any existing authentication and other proxy settings. If a more specific
location handles `/api/v1/events`, apply the body limit there instead. Nginx's
`gzip on` response setting is not a substitute for decoding request bodies.

Failed uploads log a WARNING with HTTP status (when available), byte/event counts,
and the first event's trace/span IDs, without logging context contents. They increment
`dropped_events`; Agent execution remains fail-open. A single event beyond the
receiver's decoded-body limit is rejected explicitly. `flush()` waits for queued
and in-flight delivery attempts, not just an empty queue; it does not guarantee
successful delivery. Previously dropped events cannot be reconstructed by this fix.

## Tests

```bash
PYTHONPYCACHEPREFIX=/private/tmp/path \
  python -m pytest -q nTrace/tests

cd nTrace/frontend
npm test
npm run build
```
