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
                parent_span_id INTEGER,
                parent_span_id_known INTEGER NOT NULL DEFAULT 1,
                token_usage_json TEXT NOT NULL DEFAULT '{}',
                token_usage_known INTEGER NOT NULL DEFAULT 1,
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
            CREATE INDEX IF NOT EXISTS traces_updated_idx ON traces(updated_at DESC);
            """
        )
        columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(events)").fetchall()
        }
        if "parent_span_id" not in columns:
            self._connection.execute("ALTER TABLE events ADD COLUMN parent_span_id INTEGER")
        if "parent_span_id_known" not in columns:
            self._connection.execute(
                "ALTER TABLE events ADD COLUMN parent_span_id_known INTEGER NOT NULL DEFAULT 0"
            )
        if "token_usage_json" not in columns:
            self._connection.execute(
                "ALTER TABLE events ADD COLUMN token_usage_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "token_usage_known" not in columns:
            self._connection.execute(
                "ALTER TABLE events ADD COLUMN token_usage_known INTEGER NOT NULL DEFAULT 0"
            )
        self._connection.execute(
            """
            UPDATE events
            SET parent_span_id=json_extract(payload_json, '$.parent_span_id'),
                parent_span_id_known=1
            WHERE parent_span_id_known=0
            """
        )
        self._connection.execute(
            """
            UPDATE events
            SET token_usage_json=COALESCE(json_extract(payload_json, '$.token_usage'), '{}'),
                token_usage_known=1
            WHERE token_usage_known=0
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
                        trace_id, span_id, event_type, timestamp, sender, agent_id,
                        parent_span_id, parent_span_id_known,
                        token_usage_json, token_usage_known, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 1, ?)
                    ON CONFLICT(trace_id, span_id, event_type) DO NOTHING
                    """,
                    (
                        trace_id,
                        int(event["span_id"]),
                        str(event["type"]),
                        timestamp,
                        str(event["sender"]),
                        int(event["agent_id"]),
                        event.get("parent_span_id"),
                        json.dumps(event.get("token_usage") or {}, ensure_ascii=False, separators=(",", ":")),
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
                candidates = [
                    candidate
                    for candidate in (
                        self._latest_unfinished_host_parent(timestamp),
                        self._latest_tool_call_parent(timestamp),
                    )
                    if candidate is not None
                ]
                parent = max(
                    candidates,
                    key=lambda candidate: _parse_time(str(candidate["candidate_at"])),
                    default=None,
                )
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

    def _latest_unfinished_host_parent(self, timestamp: str) -> dict[str, Any] | None:
        """Return the newest genuinely active host candidate before ``timestamp``."""

        row = self._connection.execute(
            """
            SELECT start.trace_id, start.span_id, start.agent_id,
                   start.timestamp AS candidate_at
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
        return dict(row) if row is not None else None

    def _latest_tool_call_parent(self, timestamp: str) -> dict[str, Any] | None:
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
                    "candidate_at": str(payload["timestamp"]),
                }
        return None

    def list_traces(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT trace_id
                FROM traces
                ORDER BY updated_at DESC
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

    def get_trace_timeline(self, trace_id: int) -> dict[str, Any] | None:
        """Return only fields required to draw a trace timeline."""

        with self._lock:
            trace = self._connection.execute(
                "SELECT trace_id, started_at, updated_at, status FROM traces WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            if trace is None:
                return None
            agent_rows = self._connection.execute(
                """
                SELECT agent_id, parent_agent_id, agent_name, activation_order, first_seen_at
                FROM agents WHERE trace_id=? ORDER BY activation_order, agent_id
                """,
                (trace_id,),
            ).fetchall()
            event_rows = self._connection.execute(
                """
                SELECT trace_id, span_id, event_type, timestamp, sender, agent_id,
                       parent_span_id
                FROM events
                WHERE trace_id=?
                ORDER BY timestamp, event_type DESC
                """,
                (trace_id,),
            ).fetchall()

        agents = [dict(row) for row in agent_rows]
        agents_by_id = {int(agent["agent_id"]): agent for agent in agents}
        events: list[dict[str, Any]] = []
        for row in event_rows:
            agent = agents_by_id[int(row["agent_id"])]
            events.append(
                {
                    "schema_version": 1,
                    "trace_id": int(row["trace_id"]),
                    "span_id": int(row["span_id"]),
                    "parent_span_id": row["parent_span_id"],
                    "agent_id": int(row["agent_id"]),
                    "parent_agent_id": agent["parent_agent_id"],
                    "agent_name": agent["agent_name"],
                    "activation_order": int(agent["activation_order"]),
                    "sender": row["sender"],
                    "type": row["event_type"],
                    "timestamp": row["timestamp"],
                }
            )
        spans = assemble_spans(events)
        timeline_span_fields = (
            "schema_version", "trace_id", "span_id", "parent_span_id",
            "agent_id", "parent_agent_id", "agent_name", "activation_order",
            "sender", "type", "started_at", "ended_at", "duration_ms", "running",
        )
        return {
            **dict(trace),
            "agent_count": len(agents),
            "span_count": len(spans),
            "agents": agents,
            "spans": [
                {field: span.get(field) for field in timeline_span_fields}
                for span in spans
            ],
        }

    def get_agent_token_statistics(
        self,
        trace_id: int,
        agent_id: int,
    ) -> dict[str, Any] | None:
        """Load token-only data for one agent and its direct child call trees."""

        with self._lock:
            selected = self._connection.execute(
                "SELECT agent_id FROM agents WHERE trace_id=? AND agent_id=?",
                (trace_id, agent_id),
            ).fetchone()
            if selected is None:
                return None
            agent_rows = self._connection.execute(
                """
                SELECT agent_id, parent_agent_id, agent_name, activation_order
                FROM agents WHERE trace_id=? ORDER BY activation_order, agent_id
                """,
                (trace_id,),
            ).fetchall()

        agents = [dict(row) for row in agent_rows]
        children_by_parent: dict[int, list[int]] = {}
        for agent in agents:
            parent = agent["parent_agent_id"]
            if parent is not None:
                children_by_parent.setdefault(int(parent), []).append(int(agent["agent_id"]))
        relevant_agent_ids = sorted(_descendant_agent_ids(agent_id, children_by_parent))
        placeholders = ",".join("?" for _ in relevant_agent_ids)

        with self._lock:
            llm_rows = self._connection.execute(
                f"""
                SELECT span_id, agent_id,
                       MIN(timestamp) AS started_at,
                       MAX(CASE WHEN event_type='start' THEN token_usage_json END) AS start_usage,
                       MAX(CASE WHEN event_type='end' THEN token_usage_json END) AS end_usage
                FROM events
                WHERE trace_id=? AND sender='llm' AND agent_id IN ({placeholders})
                GROUP BY span_id, agent_id
                ORDER BY started_at, span_id
                """,
                (trace_id, *relevant_agent_ids),
            ).fetchall()
            host_rows = self._connection.execute(
                f"""
                SELECT agent_id, span_id, timestamp
                FROM events
                WHERE trace_id=? AND sender='host' AND event_type='start'
                  AND agent_id IN ({placeholders})
                ORDER BY timestamp, span_id
                """,
                (trace_id, *relevant_agent_ids),
            ).fetchall()

        calls: list[dict[str, Any]] = []
        for row in llm_rows:
            end_usage = json.loads(row["end_usage"] or "{}")
            start_usage = json.loads(row["start_usage"] or "{}")
            calls.append(
                {
                    "spanId": int(row["span_id"]),
                    "agentId": int(row["agent_id"]),
                    "startedAt": str(row["started_at"]),
                    **_token_cost_breakdown(end_usage or start_usage),
                }
            )

        own_calls = [call for call in calls if call["agentId"] == agent_id]
        llm_points = [
            {
                **call,
                "index": index,
                "label": f"LLM call {index}",
                "llmCalls": 1,
            }
            for index, call in enumerate(own_calls, 1)
        ]
        first_host_by_agent: dict[int, int] = {}
        for row in host_rows:
            first_host_by_agent.setdefault(int(row["agent_id"]), int(row["span_id"]))

        direct_children = sorted(
            (agent for agent in agents if agent["parent_agent_id"] == agent_id),
            key=lambda agent: (int(agent["activation_order"]), int(agent["agent_id"])),
        )
        subagent_points: list[dict[str, Any]] = []
        for index, child in enumerate(direct_children, 1):
            child_id = int(child["agent_id"])
            target_span = first_host_by_agent.get(child_id)
            if target_span is None:
                continue
            descendants = _descendant_agent_ids(child_id, children_by_parent)
            branch_calls = [call for call in calls if call["agentId"] in descendants]
            total = _sum_token_costs(branch_calls)
            subagent_points.append(
                {
                    **total,
                    "index": index,
                    "spanId": target_span,
                    "agentId": child_id,
                    "label": str(child["agent_name"]),
                    "llmCalls": len(branch_calls),
                }
            )

        return {
            "agentId": agent_id,
            "llmCalls": llm_points,
            "subagentCalls": subagent_points,
            "totalCost": sum(point["weightedCost"] for point in llm_points)
            + sum(point["weightedCost"] for point in subagent_points),
        }

    def get_span(self, trace_id: int, span_id: int) -> dict[str, Any] | None:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json FROM events
                WHERE trace_id=? AND span_id=?
                ORDER BY timestamp, event_type DESC
                """,
                (trace_id, span_id),
            ).fetchall()
        spans = assemble_spans([json.loads(row["payload_json"]) for row in rows])
        return spans[0] if spans else None

    def get_span_details(self, trace_id: int, span_id: int) -> dict[str, Any] | None:
        """Return non-input span fields loaded by the expandable detail sections."""

        span = self.get_span(trace_id, span_id)
        if span is None:
            return None
        return {
            key: value
            for key, value in span.items()
            if key not in {"user_inputs", "start_event", "end_event"}
        }

    def get_span_user_inputs(
        self,
        trace_id: int,
        span_id: int,
        *,
        offset: int = 0,
        limit: int = 10,
    ) -> dict[str, Any] | None:
        """Page user inputs newest-first so older items can be appended on scroll."""

        span = self.get_span(trace_id, span_id)
        if span is None:
            return None
        raw_inputs = span.get("user_inputs")
        inputs = raw_inputs if isinstance(raw_inputs, list) else []
        newest_first = list(reversed(inputs))
        start = max(0, int(offset))
        size = max(1, min(100, int(limit)))
        items = newest_first[start:start + size]
        return {
            "items": items,
            "offset": start,
            "limit": size,
            "total": len(inputs),
            "has_more": start + len(items) < len(inputs),
        }

    def delete_trace(self, trace_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM traces WHERE trace_id=?",
                (trace_id,),
            )
            self._connection.execute(
                "DELETE FROM trace_contexts WHERE root_trace_id=? OR source_trace_id=?",
                (trace_id, trace_id),
            )
        return bool(cursor.rowcount)


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
    spans.sort(key=lambda span: (str(span.get("started_at") or ""), int(span["span_id"])))
    previous_by_lane: dict[tuple[int, str], dict[str, Any]] = {}
    for span in spans:
        lane = (int(span.get("agent_id") or 0), str(span.get("sender") or ""))
        previous = previous_by_lane.get(lane)
        next_started_at = span.get("started_at")
        if previous is not None and previous["running"] and next_started_at:
            previous_started_at = previous.get("started_at")
            previous["ended_at"] = next_started_at
            previous["running"] = False
            if previous_started_at:
                try:
                    previous["duration_ms"] = max(
                        0.0,
                        (_parse_time(next_started_at) - _parse_time(previous_started_at)).total_seconds() * 1000,
                    )
                except ValueError:
                    previous["duration_ms"] = None
        previous_by_lane[lane] = span
    return spans


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _token_number(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _token_cost_breakdown(usage: dict[str, Any]) -> dict[str, int]:
    details = usage.get("details") if isinstance(usage.get("details"), dict) else {}
    input_details = details.get("input_token_details")
    if not isinstance(input_details, dict):
        input_details = usage.get("input_token_details")
    if not isinstance(input_details, dict):
        input_details = {}
    input_tokens = _token_number(usage.get("input_tokens", details.get("input_tokens")))
    output_tokens = _token_number(usage.get("output_tokens", details.get("output_tokens")))
    cached_tokens = min(input_tokens, _token_number(input_details.get("cached_tokens")))
    uncached_tokens = input_tokens - cached_tokens
    return {
        "inputTokens": input_tokens,
        "uncachedInputTokens": uncached_tokens,
        "outputTokens": output_tokens,
        "cachedTokens": cached_tokens,
        "weightedCost": output_tokens * 100 + cached_tokens + uncached_tokens * 50,
    }


def _descendant_agent_ids(root_agent_id: int, children_by_parent: dict[int, list[int]]) -> set[int]:
    result: set[int] = set()
    pending = [root_agent_id]
    while pending:
        current = pending.pop()
        if current in result:
            continue
        result.add(current)
        pending.extend(children_by_parent.get(current, []))
    return result


def _sum_token_costs(calls: list[dict[str, Any]]) -> dict[str, int]:
    fields = (
        "inputTokens",
        "uncachedInputTokens",
        "outputTokens",
        "cachedTokens",
        "weightedCost",
    )
    return {field: sum(int(call[field]) for call in calls) for field in fields}


__all__ = ["TraceStorage", "assemble_spans"]
