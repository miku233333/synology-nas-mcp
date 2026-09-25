"""Allowlisted NAS storage status snapshots."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

_UTC = timezone.utc  # noqa: UP017 - DSM's bundled Python is 3.8.

SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 32 * 1024
MAX_ROWS = 32
MAX_SUGGESTIONS = 8

_SECTIONS = {
    "pools": "storagePools",
    "volumes": "volumes",
    "disks": "disks",
    "ssd_caches": "ssdCaches",
}
_FIELDS: dict[str, tuple[tuple[str, tuple[str, ...], Literal["text", "number"]], ...]] = {
    "pools": (
        ("id", ("id",), "text"),
        ("status", ("status",), "text"),
        ("summary_status", ("summary_status",), "text"),
        ("raid_type", ("raidType",), "text"),
        ("cache_status", ("cacheStatus",), "text"),
        ("total_size_value", ("size", "total"), "number"),
    ),
    "volumes": (
        ("id", ("id",), "text"),
        ("status", ("status",), "text"),
        ("file_system", ("fs_type",), "text"),
        ("total_size_value", ("size", "total"), "number"),
        ("used_size_value", ("size", "used"), "number"),
    ),
    "disks": (
        ("id", ("id",), "text"),
        ("model", ("model",), "text"),
        ("status", ("status",), "text"),
        ("overview_status", ("overview_status",), "text"),
        ("smart_status", ("smart_status",), "text"),
        ("temperature_value", ("temp",), "number"),
        ("capacity_value", ("size_total",), "number"),
        ("remaining_life_value", ("remain_life", "value"), "number"),
    ),
    "ssd_caches": (
        ("id", ("id",), "text"),
        ("status", ("status",), "text"),
        ("summary_status", ("summary_status",), "text"),
        ("mode", ("mode",), "text"),
        ("total_size_value", ("size", "total"), "number"),
        ("occupied_size_value", ("size", "occupied"), "number"),
        ("reusable_size_value", ("size", "reusable"), "number"),
        ("read_hit_rate_value", ("hit_rate",), "number"),
        ("write_hit_rate_value", ("hit_rate_write",), "number"),
    ),
}
_OUTPUT_TYPES = {
    section: {name: kind for name, _, kind in fields} for section, fields in _FIELDS.items()
}


class StatusSnapshotError(ValueError):
    """A safe, user-facing snapshot error."""


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    if any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in value):
        return None
    return value


def _token(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,64}", value):
        return value
    return None


def _number(value: object) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,24}(?:\.\d{1,6})?", value):
        return float(value) if "." in value else int(value)
    return None


def _at(value: Mapping[str, Any], path: tuple[str, ...]) -> object:
    current: object = value
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _suggestions(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    suggestions = []
    for item in value[:MAX_SUGGESTIONS]:
        if not isinstance(item, Mapping):
            continue
        code = _token(item.get("str"))
        if code is None:
            continue
        suggestion = {"str": code}
        for key in ("type", "section"):
            if (cleaned := _token(item.get(key))) is not None:
                suggestion[key] = cleaned
        suggestions.append(suggestion)
    return suggestions


def snapshot_from_dsm(data: object, captured_at: datetime | None = None) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise StatusSnapshotError("NAS status data is invalid")
    timestamp = captured_at or datetime.now(_UTC)
    if timestamp.tzinfo is None:
        raise StatusSnapshotError("NAS status timestamp is invalid")
    snapshot: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": timestamp.astimezone(_UTC).isoformat(timespec="seconds"),
    }
    truncated: dict[str, bool] = {}
    for section, source_key in _SECTIONS.items():
        source = data.get(source_key)
        if section == "ssd_caches" and not isinstance(source, list):
            snapshot[section] = []
            truncated[section] = False
            snapshot["ssd_cache_data_status"] = "unavailable"
            continue
        if not isinstance(source, list):
            raise StatusSnapshotError("NAS status data is invalid")
        rows = []
        for item in source[:MAX_ROWS]:
            if not isinstance(item, Mapping):
                continue
            row: dict[str, Any] = {}
            for name, path, kind in _FIELDS[section]:
                value = _at(item, path)
                cleaned = _text(value) if kind == "text" else _number(value)
                if cleaned is not None:
                    row[name] = cleaned
            if section == "disks":
                life = item.get("remain_life")
                if isinstance(life, Mapping) and isinstance(life.get("trustable"), bool):
                    row["remaining_life_trustable"] = life["trustable"]
                if (
                    row.get("remaining_life_trustable") is not True
                    or row.get("remaining_life_value", -1) < 0
                ):
                    row.pop("remaining_life_value", None)
            if section == "pools":
                suggestions = _suggestions(item.get("suggestions"))
                if suggestions:
                    row["suggestions"] = suggestions
            rows.append(row)
        snapshot[section] = rows
        truncated[section] = len(source) > MAX_ROWS
        if section == "ssd_caches":
            snapshot["ssd_cache_data_status"] = "reported" if rows else "empty_or_unavailable"
    snapshot["truncated"] = truncated
    return snapshot


def _validate_document(document: object) -> dict[str, Any]:
    if not isinstance(document, Mapping) or type(document.get("schema_version")) is not int:
        raise StatusSnapshotError("NAS status snapshot is invalid")
    if document["schema_version"] != SCHEMA_VERSION:
        raise StatusSnapshotError("NAS status snapshot has an unsupported schema")
    captured_at = document.get("captured_at")
    if not isinstance(captured_at, str):
        raise StatusSnapshotError("NAS status snapshot is invalid")
    try:
        timestamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError:
        raise StatusSnapshotError("NAS status snapshot is invalid") from None
    if timestamp.tzinfo is None:
        raise StatusSnapshotError("NAS status snapshot is invalid")
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "captured_at": timestamp.astimezone(_UTC).isoformat(timespec="seconds"),
    }
    for section in _SECTIONS:
        rows = document.get(section)
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise StatusSnapshotError("NAS status snapshot is invalid")
        clean_rows = []
        for item in rows:
            if not isinstance(item, Mapping):
                raise StatusSnapshotError("NAS status snapshot is invalid")
            row = {}
            for key, kind in _OUTPUT_TYPES[section].items():
                cleaned = _text(item.get(key)) if kind == "text" else _number(item.get(key))
                if cleaned is not None:
                    row[key] = cleaned
            if section == "disks" and isinstance(item.get("remaining_life_trustable"), bool):
                row["remaining_life_trustable"] = item["remaining_life_trustable"]
            if section == "disks" and (
                row.get("remaining_life_trustable") is not True
                or row.get("remaining_life_value", -1) < 0
            ):
                row.pop("remaining_life_value", None)
            if section == "pools":
                suggestions = _suggestions(item.get("suggestions"))
                if suggestions:
                    row["suggestions"] = suggestions
            clean_rows.append(row)
        result[section] = clean_rows
    flags = document.get("truncated")
    if not isinstance(flags, Mapping) or any(
        not isinstance(flags.get(section), bool) for section in _SECTIONS
    ):
        raise StatusSnapshotError("NAS status snapshot is invalid")
    result["truncated"] = {section: flags[section] for section in _SECTIONS}
    cache_data_status = document.get("ssd_cache_data_status")
    if cache_data_status not in {"reported", "empty_or_unavailable", "unavailable"} or (
        bool(result["ssd_caches"]) != (cache_data_status == "reported")
    ):
        raise StatusSnapshotError("NAS status snapshot is invalid")
    result["ssd_cache_data_status"] = cache_data_status
    return result


def read_status_snapshot(path: Path, max_age_seconds: int = 600) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_SNAPSHOT_BYTES:
                raise StatusSnapshotError("NAS status snapshot is invalid")
            now = time.time()
            if now - details.st_mtime > max_age_seconds or details.st_mtime > now + 30:
                raise StatusSnapshotError("NAS status snapshot is stale")
            content = stream.read(MAX_SNAPSHOT_BYTES + 1)
    except OSError:
        raise StatusSnapshotError("NAS status snapshot unavailable") from None
    if len(content) > MAX_SNAPSHOT_BYTES:
        raise StatusSnapshotError("NAS status snapshot is invalid")
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise StatusSnapshotError("NAS status snapshot is invalid") from None
    snapshot = _validate_document(document)
    captured_at = datetime.fromisoformat(snapshot["captured_at"]).timestamp()
    if now - captured_at > max_age_seconds or captured_at > now + 30:
        raise StatusSnapshotError("NAS status snapshot is stale")
    return snapshot
