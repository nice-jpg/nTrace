"""FastAPI service for receiving and visualizing nTrace events."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import gzip
import io
import os
from pathlib import Path
from typing import Any, Literal
import zlib

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from pydantic import field_validator, model_validator

from .ids import MAX_SAFE_INTEGER
from .storage import TraceStorage
from .snapshots import MissingSnapshot

DEFAULT_DATABASE = Path(__file__).resolve().parent / "data" / "ntrace.sqlite3"
DEFAULT_STATIC = Path(__file__).resolve().parent / "frontend" / "dist"
MAX_EVENT_BODY_BYTES = 64 * 1024 * 1024


class EventRequest(Request):
    """Decode compressed telemetry before FastAPI's normal schema validation."""

    async def body(self) -> bytes:
        if not hasattr(self, "_body"):
            encoding = self.headers.get("content-encoding", "identity").strip().lower()
            if encoding not in {"identity", "gzip"}:
                raise HTTPException(415, "Unsupported Content-Encoding")
            body = bytearray()
            async for chunk in self.stream():
                if len(body) + len(chunk) > MAX_EVENT_BODY_BYTES:
                    raise HTTPException(413, "Event packet exceeds body limit")
                body.extend(chunk)
            if encoding == "gzip":
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                        decoded = compressed.read(MAX_EVENT_BODY_BYTES + 1)
                except (OSError, EOFError, zlib.error) as error:
                    raise HTTPException(400, "Invalid gzip event packet") from error
                if len(decoded) > MAX_EVENT_BODY_BYTES:
                    raise HTTPException(413, "Decoded event packet exceeds body limit")
                self._body = decoded
            else:
                self._body = bytes(body)
        return self._body


class EventRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def receive(request: Request):
            return await handler(EventRequest(request.scope, request.receive))

        return receive


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
    sender: Literal["host", "llm", "tool"]
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


class SnapshotPacket(BaseModel):
    snapshot_version: Literal[1]
    events: list[dict[str, Any]] = Field(min_length=1, max_length=1_000)
    objects: dict[str, Any] = Field(default_factory=dict)


class TraceMetadataUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=256)
    favorite: bool | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("display_name must not be blank")
        return value

    @model_validator(mode="after")
    def require_change(self) -> "TraceMetadataUpdate":
        if self.display_name is None and self.favorite is None:
            raise ValueError("At least one trace field must be provided")
        return self


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
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    events_router = APIRouter(route_class=EventRoute)

    @events_router.post("/api/v1/events")
    async def receive_events(packet: EventPacket | TraceEvent) -> dict[str, int]:
        models = packet.events if isinstance(packet, EventPacket) else [packet]
        events = [model.model_dump(mode="json") for model in models]
        stored = storage.put_events(events)
        for event in stored:
            await stream.broadcast({"kind": "event.created", "event": _timeline_event(event)})
        return {"accepted": len(events), "stored": len(stored)}

    @events_router.post("/api/v1/snapshot-events")
    async def receive_snapshots(packet: SnapshotPacket) -> dict[str, int]:
        try:
            stored = storage.put_snapshot_events(
                packet.events, packet.objects,
                lambda event: TraceEvent.model_validate(event).model_dump(mode="json"),
            )
        except MissingSnapshot as error:
            raise HTTPException(409, str(error)) from error
        except (ValueError, TypeError, RecursionError) as error:
            raise HTTPException(422, "Invalid context snapshot packet") from error
        for event in stored:
            await stream.broadcast({"kind": "event.created", "event": _timeline_event(event)})
        return {"accepted": len(packet.events), "stored": len(stored)}

    app.include_router(events_router)

    @app.get("/api/v1/traces")
    async def list_traces(
        limit: int = Query(default=100, ge=1, le=1_000),
        favorite: bool = Query(default=False),
    ) -> dict[str, Any]:
        return {"traces": storage.list_traces(limit, favorite=favorite)}

    @app.patch("/api/v1/traces/{trace_id}")
    async def update_trace(trace_id: int, update: TraceMetadataUpdate) -> dict[str, Any]:
        trace = storage.update_trace_metadata(
            trace_id,
            display_name=update.display_name,
            favorite=update.favorite,
        )
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        await stream.broadcast({"kind": "trace.updated", "trace": trace})
        return trace

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

    @app.get("/api/v1/traces/{trace_id}/spans/{span_id}/details")
    async def get_span_details(trace_id: int, span_id: int) -> dict[str, Any]:
        span = storage.get_span_details(trace_id, span_id)
        if span is None:
            raise HTTPException(status_code=404, detail="Span not found")
        return span

    @app.get("/api/v1/traces/{trace_id}/spans/{span_id}/user-inputs")
    async def get_span_user_inputs(
        trace_id: int,
        span_id: int,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict[str, Any]:
        page = storage.get_span_user_inputs(
            trace_id,
            span_id,
            offset=offset,
            limit=limit,
        )
        if page is None:
            raise HTTPException(status_code=404, detail="Span not found")
        return page

    @app.get("/api/v1/traces/{trace_id}/agents/{agent_id}/token-stats")
    async def get_agent_token_statistics(trace_id: int, agent_id: int) -> dict[str, Any]:
        statistics = storage.get_agent_token_statistics(trace_id, agent_id)
        if statistics is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return statistics

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


def _timeline_event(event: dict[str, Any]) -> dict[str, Any]:
    """Strip heavy payload fields before broadcasting timeline invalidations."""

    fields = (
        "schema_version",
        "trace_id",
        "span_id",
        "parent_span_id",
        "agent_id",
        "parent_agent_id",
        "agent_name",
        "activation_order",
        "sender",
        "type",
        "timestamp",
    )
    return {field: event.get(field) for field in fields}


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
