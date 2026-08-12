"""SQLite persistence and span assembly for nTrace."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any


class TraceStorage:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS traces (
                trace_id INTEGER PRIMARY KEY,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running'
            );
            CREATE TABLE IF NOT EXISTS agents (
                trace_id INTEGER NOT NULL,
                agent_id INTEGER NOT NULL,
                parent_agent_id INTEGER,
                agent_name TEXT NOT NULL,
                activation_order INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY (trace_id, agent_id),
                FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS events (
                trace_id INTEGER NOT NULL,
                span_id INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('start', 'end')),
                timestamp TEXT NOT NULL,
                sender TEXT NOT NULL CHECK(sender IN ('host', 'llm')),
                agent_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (trace_id, span_id, event_type),
                FOREIGN KEY (trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS trace_contexts (
                source_trace_id INTEGER PRIMARY KEY,
                root_trace_id INTEGER NOT NULL,
                parent_span_id INTEGER,
                parent_agent_id INTEGER,
                assigned_agent_id INTEGER NOT NULL,
                activation_order INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_trace_time_idx ON events(trace_id, timestamp);
            CREATE INDEX IF NOT EXISTS agents_trace_order_idx ON agents(trace_id, activation_order);
            CREATE INDEX IF NOT EXISTS trace_contexts_root_idx ON trace_contexts(root_trace_id);
            """
        )
        self._connection.commit()

    def put_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stored: list[dict[str, Any]] = []
        with self._lock, self._connection:
            for raw_event in events:
                event = self._contextualize_event(raw_event)
                trace_id = int(event["trace_id"])
                timestamp = str(event["timestamp"])
                self._connection.execute(
                    """
                    INSERT INTO traces(trace_id, started_at, updated_at, status)
                    VALUES (?, ?, ?, 'running')
                    ON CONFLICT(trace_id) DO UPDATE SET
                        status='running',
                        started_at=CASE
                            WHEN excluded.started_at < traces.started_at THEN excluded.started_at
                            ELSE traces.started_at
                        END,
                        updated_at=CASE
                            WHEN excluded.updated_at > traces.updated_at THEN excluded.updated_at
                            ELSE traces.updated_at
                        END
                    """,
                    (trace_id, timestamp, timestamp),
                )
                self._connection.execute(
                    """
                    INSERT INTO agents(
                        trace_id, agent_id, parent_agent_id, agent_name,
                        activation_order, first_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trace_id, agent_id) DO UPDATE SET
                        parent_agent_id=excluded.parent_agent_id,
                        agent_name=excluded.agent_name,
                        activation_order=excluded.activation_order,
                        first_seen_at=CASE
                            WHEN excluded.first_seen_at < agents.first_seen_at THEN excluded.first_seen_at
                            ELSE agents.first_seen_at
                        END
                    """,
                    (
                        trace_id,
                        int(event["agent_id"]),
                        event.get("parent_agent_id"),
                        str(event.get("agent_name") or f"agent-{event['agent_id']}"),
                        int(event.get("activation_order") or event["agent_id"]),
                        timestamp,
                    ),
                )
                cursor = self._connection.execute(
                    """
                    INSERT INTO events(
                        trace_id, span_id, event_type, timestamp, sender, agent_id, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trace_id, span_id, event_type) DO NOTHING
                    """,
                    (
                        trace_id,
                        int(event["span_id"]),
                        str(event["type"]),
                        timestamp,
                        str(event["sender"]),
                        int(event["agent_id"]),
                        json.dumps(event, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                if cursor.rowcount:
                    stored.append(event)
        return stored

    def _contextualize_event(self, raw_event: dict[str, Any]) -> dict[str, Any]:
        """Map an agent-local trace into the active server-side invocation tree."""

        source_trace_id = int(raw_event["trace_id"])
        timestamp = str(raw_event["timestamp"])
        context = self._connection.execute(
            """
            SELECT root_trace_id, parent_span_id, parent_agent_id,
                   assigned_agent_id, activation_order
            FROM trace_contexts WHERE source_trace_id=?
            """,
            (source_trace_id,),
        ).fetchone()
        if context is None:
            raw_data = raw_event.get("data")
            starts_user_turn = (
                isinstance(raw_data, dict)
                and raw_data.get("trace_boundary") == "user_input"
            )
            parent = None
            if not starts_user_turn:
                parent = self._connection.execute(
                    """
                    SELECT start.trace_id, start.span_id, start.agent_id
                    FROM events AS start
                    LEFT JOIN events AS finish
                      ON finish.trace_id=start.trace_id
                     AND finish.span_id=start.span_id
                     AND finish.event_type='end'
                    WHERE start.sender='host'
                      AND start.event_type='start'
                      AND start.timestamp <= ?
                      AND finish.span_id IS NULL
                    ORDER BY start.timestamp DESC, start.rowid DESC
                    LIMIT 1
                    """,
                    (timestamp,),
                ).fetchone()
                if parent is None:
                    parent = self._latest_tool_call_parent(timestamp)
            if parent is None:
                root_trace_id = source_trace_id
                parent_span_id = None
                parent_agent_id = None
                assigned_agent_id = 1
                activation_order = 1
            else:
                root_trace_id = int(parent["trace_id"])
                parent_span_id = int(parent["span_id"])
                parent_agent_id = int(parent["agent_id"])
                next_agent = self._connection.execute(
                    "SELECT COALESCE(MAX(agent_id), 0) + 1 FROM agents WHERE trace_id=?",
                    (root_trace_id,),
                ).fetchone()[0]
                assigned_agent_id = int(next_agent)
                activation_order = assigned_agent_id
            self._connection.execute(
                """
                INSERT INTO trace_contexts(
                    source_trace_id, root_trace_id, parent_span_id, parent_agent_id,
                    assigned_agent_id, activation_order, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_trace_id,
                    root_trace_id,
                    parent_span_id,
                    parent_agent_id,
                    assigned_agent_id,
                    activation_order,
                    timestamp,
                ),
            )
            context = {
                "root_trace_id": root_trace_id,
                "parent_span_id": parent_span_id,
                "parent_agent_id": parent_agent_id,
                "assigned_agent_id": assigned_agent_id,
                "activation_order": activation_order,
            }

        event = dict(raw_event)
        data = event.get("data")
        data = dict(data) if isinstance(data, dict) else {"client_data": data}
        data["source_trace_id"] = source_trace_id
        event["data"] = data
        event["trace_id"] = int(context["root_trace_id"])
        event["agent_id"] = int(context["assigned_agent_id"])
        event["activation_order"] = int(context["activation_order"])
        event["parent_agent_id"] = context["parent_agent_id"]
        if event.get("sender") == "host":
            event["parent_span_id"] = context["parent_span_id"]
        return event

    def _latest_tool_call_parent(self, timestamp: str) -> dict[str, int] | None:
        """Find the host span whose model response most recently started a tool."""

        rows = self._connection.execute(
            """
            SELECT event.trace_id, event.agent_id, event.payload_json
            FROM events AS event
            JOIN traces AS trace ON trace.trace_id=event.trace_id
            WHERE event.sender='llm'
              AND event.event_type='end'
              AND event.timestamp <= ?
              AND trace.status='running'
            ORDER BY event.timestamp DESC, event.rowid DESC
            LIMIT 50
            """,
            (timestamp,),
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            parent_span_id = payload.get("parent_span_id")
            if payload.get("tools_called") and parent_span_id is not None:
                return {
                    "trace_id": int(row["trace_id"]),
                    "span_id": int(parent_span_id),
                    "agent_id": int(row["agent_id"]),
                }
        return None

    def list_traces(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT t.trace_id, t.started_at, t.updated_at, t.status,
                       COUNT(DISTINCT a.agent_id) AS agent_count,
                       COUNT(DISTINCT e.span_id) AS span_count
                FROM traces t
                LEFT JOIN agents a ON a.trace_id=t.trace_id
                LEFT JOIN events e ON e.trace_id=t.trace_id
                GROUP BY t.trace_id
                ORDER BY t.updated_at DESC
                LIMIT ?
                """,
                (max(1, min(1_000, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trace(self, trace_id: int) -> dict[str, Any] | None:
        with self._lock:
            trace = self._connection.execute(
                "SELECT trace_id, started_at, updated_at, status FROM traces WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            if trace is None:
                return None
            agents = self._connection.execute(
                """
                SELECT agent_id, parent_agent_id, agent_name, activation_order, first_seen_at
                FROM agents WHERE trace_id=? ORDER BY activation_order, agent_id
                """,
                (trace_id,),
            ).fetchall()
            rows = self._connection.execute(
                "SELECT payload_json FROM events WHERE trace_id=? ORDER BY timestamp, event_type DESC",
                (trace_id,),
            ).fetchall()
        events = [json.loads(row["payload_json"]) for row in rows]
        return {
            **dict(trace),
            "agents": [dict(row) for row in agents],
            "events": events,
            "spans": assemble_spans(events),
        }


def assemble_spans(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(int(event["span_id"]), {})[str(event["type"])] = event

    spans: list[dict[str, Any]] = []
    for span_id, pair in grouped.items():
        start = pair.get("start")
        end = pair.get("end")
        base = start or end
        if base is None:
            continue
        merged = dict(start or {})
        for key, value in (end or {}).items():
            if value not in (None, "", [], {}):
                merged[key] = value
        started_at = (start or end or {}).get("timestamp")
        ended_at = end.get("timestamp") if end else None
        duration_ms = None
        if started_at and ended_at:
            try:
                duration_ms = max(0.0, (_parse_time(ended_at) - _parse_time(started_at)).total_seconds() * 1000)
            except ValueError:
                duration_ms = None
        spans.append(
            {
                **merged,
                "span_id": span_id,
                "type": "span",
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms,
                "running": end is None,
                "start_event": start,
                "end_event": end,
            }
        )
    return sorted(spans, key=lambda span: (str(span.get("started_at") or ""), int(span["span_id"])))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


__all__ = ["TraceStorage", "assemble_spans"]
