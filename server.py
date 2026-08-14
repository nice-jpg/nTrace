"""FastAPI service for receiving and visualizing nTrace events."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .ids import MAX_SAFE_INTEGER
from .storage import TraceStorage

DEFAULT_DATABASE = Path(__file__).resolve().parent / "data" / "ntrace.sqlite3"
DEFAULT_STATIC = Path(__file__).resolve().parent / "frontend" / "dist"


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal[1] = 1
    trace_id: int = Field(ge=0, le=MAX_SAFE_INTEGER)
    span_id: int = Field(ge=0, le=MAX_SAFE_INTEGER)
    parent_span_id: int | None = Field(default=None, ge=0, le=MAX_SAFE_INTEGER)
    agent_id: int = Field(ge=1, le=MAX_SAFE_INTEGER)
    parent_agent_id: int | None = Field(default=None, ge=1, le=MAX_SAFE_INTEGER)
    agent_name: str = Field(min_length=1, max_length=256)
    activation_order: int = Field(ge=1)
    sender: Literal["host", "llm"]
    type: Literal["start", "end"]
    timestamp: datetime
    system_prompt: Any = None
    user_inputs: list[Any] = Field(default_factory=list)
    output: Any = None
    tools: list[Any] = Field(default_factory=list)
    tools_called: list[Any] = Field(default_factory=list)
    tool_call_results: list[Any] = Field(default_factory=list)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    data: Any = Field(default_factory=dict)


class EventPacket(BaseModel):
    events: list[TraceEvent] = Field(min_length=1, max_length=1_000)


class StreamHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        async with self._lock:
            clients = list(self._clients)
        stale: list[WebSocket] = []
        for websocket in clients:
            try:
                await websocket.send_json(payload)
            except Exception:  # noqa: BLE001 - stale sockets are removed below.
                stale.append(websocket)
        if stale:
            async with self._lock:
                for websocket in stale:
                    self._clients.discard(websocket)


def create_app(
    *,
    database_path: str | Path | None = None,
    static_dir: str | Path | None = None,
) -> FastAPI:
    storage = TraceStorage(database_path or os.getenv("NTRACE_DATABASE_PATH") or DEFAULT_DATABASE)
    stream = StreamHub()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        storage.close()

    app = FastAPI(title="nTrace", version="1.0.0", lifespan=lifespan)
    app.state.storage = storage
    app.state.stream = stream
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/events")
    async def receive_events(packet: EventPacket | TraceEvent) -> dict[str, int]:
        models = packet.events if isinstance(packet, EventPacket) else [packet]
        events = [model.model_dump(mode="json") for model in models]
        stored = storage.put_events(events)
        for event in stored:
            await stream.broadcast({"kind": "event.created", "event": event})
        return {"accepted": len(events), "stored": len(stored)}

    @app.get("/api/v1/traces")
    async def list_traces(limit: int = Query(default=100, ge=1, le=1_000)) -> dict[str, Any]:
        return {"traces": storage.list_traces(limit)}

    @app.get("/api/v1/traces/{trace_id}")
    async def get_trace(trace_id: int) -> dict[str, Any]:
        trace = storage.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace

    @app.get("/api/v1/traces/{trace_id}/timeline")
    async def get_trace_timeline(trace_id: int) -> dict[str, Any]:
        trace = storage.get_trace_timeline(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace

    @app.get("/api/v1/traces/{trace_id}/spans/{span_id}")
    async def get_span(trace_id: int, span_id: int) -> dict[str, Any]:
        span = storage.get_span(trace_id, span_id)
        if span is None:
            raise HTTPException(status_code=404, detail="Span not found")
        return span

    @app.delete("/api/v1/traces/{trace_id}")
    async def delete_trace(trace_id: int) -> dict[str, int]:
        if not storage.delete_trace(trace_id):
            raise HTTPException(status_code=404, detail="Trace not found")
        await stream.broadcast({"kind": "trace.deleted", "trace_id": trace_id})
        return {"deleted": trace_id}

    @app.websocket("/api/v1/stream")
    async def websocket_stream(websocket: WebSocket) -> None:
        await stream.connect(websocket)
        await websocket.send_json({"kind": "stream.ready"})
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await stream.disconnect(websocket)

    assets = Path(static_dir) if static_dir is not None else DEFAULT_STATIC
    if assets.is_dir():
        asset_dir = assets / "assets"
        if asset_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=asset_dir), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str) -> FileResponse:
            candidate = (assets / path).resolve()
            if path and candidate.is_file() and assets.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(assets / "index.html")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local nTrace service.")
    parser.add_argument("--host", default=os.getenv("NTRACE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NTRACE_PORT", "8765")))
    parser.add_argument("--database", default=os.getenv("NTRACE_DATABASE_PATH", str(DEFAULT_DATABASE)))
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(create_app(database_path=args.database), host=args.host, port=args.port)


app = create_app() if __name__ != "__main__" else None


if __name__ == "__main__":
    main()
