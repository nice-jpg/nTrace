"""Content-addressed context objects shared by the sender and persistent receiver."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

REF = "$ntrace_snapshot"
LOGGER = logging.getLogger("nTrace.snapshots")
FIELDS = ("system_prompt", "user_inputs", "output", "tools", "tools_called",
          "tool_call_results", "token_usage", "data")


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def image_paths(value: Any) -> Any:
    """Keep remote/local paths; materialize inline images locally, never upload pixels."""
    if isinstance(value, str) and value.startswith("data:image/"):
        header, _, encoded = value.partition(",")
        extension = header.split("/", 1)[1].split(";", 1)[0]
        extension = extension if extension in {"png", "jpeg", "jpg", "webp", "gif"} else "bin"
        directory = Path(os.getenv("NTRACE_IMAGE_DIR") or
                         Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / "ntrace" / "images")
        # Hash the encoded string first so repeated historical images need no decoding or writes.
        path = directory.resolve() / (hashlib.sha256(value.encode()).hexdigest() + "." + extension)
        try:
            if not path.exists():
                directory.mkdir(parents=True, exist_ok=True)
                pixels = base64.b64decode(encoded, validate=True)
                try:
                    with path.open("xb") as image:
                        image.write(pixels)
                except FileExistsError:
                    pass
        except (OSError, ValueError):
            # A failed local image cache must not lose host/LLM timing events.
            LOGGER.warning("nTrace image cache unavailable or image invalid; omitting pixels")
            return {"type": "image_path", "path": None, "unavailable": True}
        return str(path)
    if isinstance(value, list):
        return [image_paths(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") == "base64" and isinstance(value.get("data"), str):
            media = value.get("media_type", "image/png")
            if str(media).startswith("image/"):
                path = image_paths(f"data:{media};base64,{value['data']}")
                return path if isinstance(path, dict) else {"type": "image_path", "path": path}
        return {key: image_paths(item) for key, item in value.items()}
    return value


def encode_events(events: list[dict]) -> tuple[list[dict], dict[str, Any]]:
    objects: dict[str, Any] = {}

    def encode(value: Any, *, force: bool = False) -> Any:
        if isinstance(value, list):
            node = [encode(item) for item in value]
        elif isinstance(value, dict):
            node = {key: encode(item) for key, item in value.items()}
        else:
            node = value
        # Small scalars/metadata stay inline; history text and structured contexts are interned.
        if isinstance(value, (dict, list, str)) and value and (force or isinstance(value, (dict, list)) or
                                                             len(dumps(node)) >= 128):
            key = digest(node)
            objects[key] = node
            return {REF: key}
        return node

    return [{key: encode(value, force=True) if key in FIELDS else value for key, value in event.items()}
            for event in events], objects


class MissingSnapshot(ValueError):
    pass


def resolve_event(event: dict, lookup: Callable[[str], Any]) -> dict:
    active: set[str] = set()
    remaining = 1_000_000
    bytes_left = 64 * 1024 * 1024

    def resolve(value: Any, depth: int = 0) -> Any:
        nonlocal remaining, bytes_left
        remaining -= 1
        if isinstance(value, str):
            bytes_left -= len(value.encode())
        if depth > 100 or remaining < 0 or bytes_left < 0:
            raise ValueError("Snapshot expansion limit exceeded")
        if isinstance(value, dict):
            if set(value) == {REF}:
                key = value[REF]
                if not isinstance(key, str) or key in active:
                    raise ValueError("Invalid snapshot reference")
                active.add(key)
                # A stored object is a literal container, so user dictionaries with REF round-trip.
                node = lookup(key)
                if isinstance(node, dict):
                    result = {k: resolve(v, depth + 1) for k, v in node.items()}
                elif isinstance(node, list):
                    result = [resolve(v, depth + 1) for v in node]
                else:
                    result = resolve(node, depth + 1)
                active.remove(key)
                return result
            return {key: resolve(item, depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item, depth + 1) for item in value]
        return value

    return {key: resolve(value) if key in FIELDS else value for key, value in event.items()}
