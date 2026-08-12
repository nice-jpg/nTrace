# nTrace

Local-first smart trace for the Bines LangChain agents. It records host and LLM spans,
persists them in SQLite, streams new events over WebSocket, and renders a multi-agent
timeline in React.

## Run

Install the Python service dependencies:

```bash
/Users/nice/Env/anaconda3/envs/bines/bin/python -m pip install -r nTrace/requirements.txt
```

Build the frontend once, then start the combined server:

```bash
cd nTrace/frontend
npm install
npm run build
cd ../..
/Users/nice/Env/anaconda3/envs/bines/bin/python -m nTrace.server
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
active trace and records the current `session_id`. `resume_turn` keeps that active trace.
Each dynamic collector owns an independent trace and does not inherit client execution
context. The server infers parent/child relationships from host-span timing and persists
the resulting trace tree.

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
`POST /api/v1/events`. History is available from `GET /api/v1/traces`, a complete
snapshot from `GET /api/v1/traces/{trace_id}`, and live updates from
`WS /api/v1/stream`.

## Tests

```bash
PYTHONPYCACHEPREFIX=/private/tmp/bines_pycache \
  /Users/nice/Env/anaconda3/envs/bines/bin/python -m pytest -q nTrace/tests

cd nTrace/frontend
npm test
npm run build
```
