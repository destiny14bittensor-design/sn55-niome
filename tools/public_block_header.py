#!/usr/bin/env python3
"""Canonicalize the public header carried by bittensor ``BlockInfo``."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


def _json_public(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_public(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_public(child) for child in value]
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def public_header(info: Any) -> dict[str, Any] | None:
    """Return only the JSON-safe public block header, never extrinsics."""
    header = getattr(info, "header", None)
    if not isinstance(header, Mapping):
        return None
    normalized = _json_public(header)
    return normalized if isinstance(normalized, dict) and normalized else None
