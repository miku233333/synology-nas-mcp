"""Allowlisted NAS health snapshots collected by a root-owned producer."""

# ruff: noqa: UP017, UP045  # DSM's bundled Python is 3.8.

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_UTC = timezone.utc

SCHEMA_VERSION = 1
MAX_HEALTH_SNAPSHOT_BYTES = 32 * 1024
MAX_INTERFACES = 16
MAX_BOND_SLAVES = 16
MAX_SERVICE_ROWS = 8
_FUTURE_TOLERANCE_SECONDS = 30
_COMMAND_TIMEOUT_SECONDS = 5
_COMMAND_OUTPUT_BYTES = 16 * 1024

_UPSC_PATHS = (Path("/bin/upsc"), Path("/usr/bin/upsc"), Path("/usr/syno/bin/upsc"))
_SYSTEMCTL_PATHS = (Path("/bin/systemctl"), Path("/usr/bin/systemctl"))
_SYNOPKG_PATHS = (Path("/usr/syno/bin/synopkg"),)
_SYS_CLASS_NET = Path("/sys/class/net")
_PROC_BONDING = Path("/proc/net/bonding")
_UPTIME = Path("/proc/uptime")
_INTERFACE_NAME = re.compile(r"(?:eth|bond)[0-9]{1,4}")
_UPSC_FLAG = re.compile(r"(?:ALARM|OL|OB|LB|RB|CHRG|DISCHRG|BYPASS|CAL|OFF|OVER|TRIM|BOOST|FSD)")
_SERVICE_STATES = {"active", "inactive", "failed", "activating", "deactivating"}
_BOND_MODE_TEXT = "IEEE 802.3ad Dynamic link aggregation"
_BOND_MODE = "802_3ad"
_SYSTEMD_UNITS = (
    "sshd.service",
    "nginx.service",
)
_PACKAGE_NAMES = (
    "ContainerManager",
    "SMBService",
    "SynologyDrive",
    "Tailscale",
    "FileStation",
    "CloudSync",
)


class HealthSnapshotError(ValueError):
    """A safe, user-facing health snapshot error."""


def _timestamp(now: Optional[datetime] = None) -> str:
    value = now or datetime.now(_UTC)
    if value.tzinfo is None:
        raise HealthSnapshotError("NAS health timestamp is invalid")
    return value.astimezone(_UTC).isoformat(timespec="seconds")


def _candidate(paths: tuple) -> Optional[Path]:
    for path in paths:
        if path.is_file() and os.access(str(path), os.X_OK):
            return path
    return None


def _run(argv: tuple, accepted_returncodes: tuple = (0,)) -> Optional[bytes]:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode not in accepted_returncodes or len(result.stdout) > _COMMAND_OUTPUT_BYTES:
        return None
    return result.stdout


def _integer(value: object, minimum: int, maximum: int) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value):
        number = int(value)
    else:
        return None
    return number if minimum <= number <= maximum else None


def _power() -> dict:
    result = {"data_status": "unavailable", "running": True, "psu_health": "unknown"}
    try:
        uptime = _UPTIME.read_text(encoding="ascii").split()[0]
        seconds = int(float(uptime))
    except (OSError, ValueError, IndexError):
        return result
    if seconds < 0:
        return result
    result["uptime_seconds"] = seconds
    result["data_status"] = "ok"
    return result


def _ups() -> dict:
    command = _candidate(_UPSC_PATHS)
    if command is None:
        return {"data_status": "unavailable"}
    output = _run((str(command), "ups@localhost"))
    if output is None:
        return {"data_status": "unavailable"}
    try:
        lines = output.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return {"data_status": "unavailable"}
    values = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key in {"ups.status", "battery.charge", "battery.runtime"}:
            values[key] = value.strip()
    flags = [flag for flag in values.get("ups.status", "").split() if _UPSC_FLAG.fullmatch(flag)]
    if not flags or len(flags) > 16:
        return {"data_status": "unavailable"}
    result = {"data_status": "ok", "status_flags": flags}
    charge = _integer(values.get("battery.charge"), 0, 100)
    if charge is not None:
        result["battery_charge_percent"] = charge
    runtime = _integer(values.get("battery.runtime"), 0, 31_536_000)
    if runtime is not None:
        result["battery_runtime_seconds"] = runtime
    return result


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _bond_details(name: str) -> dict:
    source = _read_text(_PROC_BONDING / name)
    if source is None or len(source) > _COMMAND_OUTPUT_BYTES:
        return {}
    result = {}
    slaves = []
    for line in source.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        value = value.strip()
        if key == "Bonding Mode" and value == _BOND_MODE_TEXT:
            result["bond_mode"] = _BOND_MODE
        elif key == "Slave Interface" and _INTERFACE_NAME.fullmatch(value):
            if value.startswith("eth") and len(slaves) < MAX_BOND_SLAVES:
                slaves.append(value)
    if slaves:
        result["slaves"] = slaves
    return result


def _network() -> dict:
    try:
        names = sorted(
            entry.name
            for entry in _SYS_CLASS_NET.iterdir()
            if _INTERFACE_NAME.fullmatch(entry.name)
        )[:MAX_INTERFACES]
    except OSError:
        return {"data_status": "unavailable", "interfaces": []}
    interfaces = []
    for name in names:
        kind = "bond" if name.startswith("bond") else "physical"
        state = _read_text(_SYS_CLASS_NET / name / "operstate")
        link_states = {"up", "down", "unknown", "dormant", "lowerlayerdown", "notpresent"}
        row = {
            "name": name,
            "kind": kind,
            "link_state": state if state in link_states else "unknown",
        }
        speed = _integer(_read_text(_SYS_CLASS_NET / name / "speed"), 0, 1_000_000)
        if speed is not None:
            row["speed_mbps"] = speed
        if kind == "bond":
            row.update(_bond_details(name))
        interfaces.append(row)
    return {"data_status": "ok", "interfaces": interfaces}


def _service_details(command: Optional[Path], unit: str) -> dict:
    if command is None:
        return {"name": unit, "kind": "service", "data_status": "unavailable"}
    output = _run((str(command), "is-active", unit), accepted_returncodes=(0, 3))
    if output is None:
        return {"name": unit, "kind": "service", "data_status": "unavailable"}
    try:
        value = output.decode("utf-8").strip()
    except UnicodeDecodeError:
        return {"name": unit, "kind": "service", "data_status": "unavailable"}
    if value not in _SERVICE_STATES:
        return {"name": unit, "kind": "service", "data_status": "unavailable"}
    return {"name": unit, "kind": "service", "data_status": "ok", "state": value}


def _package_details(command: Optional[Path], package: str) -> dict:
    if command is None:
        return {"name": package, "kind": "package", "data_status": "unavailable"}
    output = _run((str(command), "status", package))
    if output is None:
        return {"name": package, "kind": "package", "data_status": "unavailable"}
    try:
        document = json.loads(output)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {"name": package, "kind": "package", "data_status": "unavailable"}
    if not isinstance(document, Mapping):
        return {"name": package, "kind": "package", "data_status": "unavailable"}
    source_state = document.get("status")
    states = {
        "running": "running",
        "stop": "stopped",
        "stopped": "stopped",
        "non_installed": "non_installed",
        "broken_by_other": "broken_by_other",
    }
    if not isinstance(source_state, str) or source_state not in states:
        return {"name": package, "kind": "package", "data_status": "unavailable"}
    return {
        "name": package,
        "kind": "package",
        "data_status": "ok",
        "state": states[source_state],
    }


def _services() -> dict:
    systemctl = _candidate(_SYSTEMCTL_PATHS)
    synopkg = _candidate(_SYNOPKG_PATHS)
    units = [
        *[_service_details(systemctl, unit) for unit in _SYSTEMD_UNITS],
        *[_package_details(synopkg, package) for package in _PACKAGE_NAMES],
    ]
    return {
        "data_status": "ok"
        if any(unit["data_status"] == "ok" for unit in units)
        else "unavailable",
        "units": units,
    }


def collect_health(captured_at: Optional[datetime] = None) -> dict:
    """Collect a bounded, allowlisted health view without changing NAS state."""
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": _timestamp(captured_at),
        "power": _power(),
        "ups": _ups(),
        "network": _network(),
        "services": _services(),
    }


def _document_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise HealthSnapshotError("NAS health snapshot is invalid")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HealthSnapshotError("NAS health snapshot is invalid") from None
    if timestamp.tzinfo is None:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    return timestamp.astimezone(_UTC)


def _exact_keys(value: Mapping, required: set, optional: set = set()) -> None:
    if not required.issubset(value) or not set(value).issubset(required | optional):
        raise HealthSnapshotError("NAS health snapshot is invalid")


def _status(value: object) -> str:
    if not isinstance(value, str) or value not in {"ok", "unavailable"}:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    return value


def _validate_power(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise HealthSnapshotError("NAS health snapshot is invalid")
    _exact_keys(value, {"data_status", "running", "psu_health"}, {"uptime_seconds"})
    status = _status(value.get("data_status"))
    if value.get("running") is not True or value.get("psu_health") != "unknown":
        raise HealthSnapshotError("NAS health snapshot is invalid")
    result = {"data_status": status, "running": True, "psu_health": "unknown"}
    uptime = _integer(value.get("uptime_seconds"), 0, 31_536_000_000)
    if status == "ok" and uptime is None:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    if uptime is not None:
        result["uptime_seconds"] = uptime
    return result


def _validate_ups(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise HealthSnapshotError("NAS health snapshot is invalid")
    _exact_keys(
        value,
        {"data_status"},
        {"status_flags", "battery_charge_percent", "battery_runtime_seconds"},
    )
    status = _status(value.get("data_status"))
    result = {"data_status": status}
    flags = value.get("status_flags")
    if flags is not None:
        if (
            not isinstance(flags, list)
            or not flags
            or len(flags) > 16
            or not all(isinstance(flag, str) and _UPSC_FLAG.fullmatch(flag) for flag in flags)
        ):
            raise HealthSnapshotError("NAS health snapshot is invalid")
        result["status_flags"] = flags
    for key, maximum in (("battery_charge_percent", 100), ("battery_runtime_seconds", 31_536_000)):
        number = _integer(value.get(key), 0, maximum)
        if value.get(key) is not None and number is None:
            raise HealthSnapshotError("NAS health snapshot is invalid")
        if number is not None:
            result[key] = number
    if status == "ok" and "status_flags" not in result:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    if status == "unavailable" and len(result) != 1:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    return result


def _validate_network(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise HealthSnapshotError("NAS health snapshot is invalid")
    _exact_keys(value, {"data_status", "interfaces"})
    status = _status(value.get("data_status"))
    rows = value.get("interfaces")
    if not isinstance(rows, list) or len(rows) > MAX_INTERFACES:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    output = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise HealthSnapshotError("NAS health snapshot is invalid")
        _exact_keys(row, {"name", "kind", "link_state"}, {"speed_mbps", "bond_mode", "slaves"})
        name = row.get("name")
        kind = row.get("kind")
        link_state = row.get("link_state")
        if (
            not isinstance(name, str)
            or not _INTERFACE_NAME.fullmatch(name)
            or not isinstance(kind, str)
            or kind not in {"physical", "bond"}
            or (kind == "bond") != name.startswith("bond")
            or not isinstance(link_state, str)
            or link_state
            not in {"up", "down", "unknown", "dormant", "lowerlayerdown", "notpresent"}
        ):
            raise HealthSnapshotError("NAS health snapshot is invalid")
        cleaned = {"name": name, "kind": kind, "link_state": link_state}
        speed = _integer(row.get("speed_mbps"), 0, 1_000_000)
        if row.get("speed_mbps") is not None and speed is None:
            raise HealthSnapshotError("NAS health snapshot is invalid")
        if speed is not None:
            cleaned["speed_mbps"] = speed
        if kind == "bond":
            mode = row.get("bond_mode")
            if mode is not None:
                if not isinstance(mode, str) or mode != _BOND_MODE:
                    raise HealthSnapshotError("NAS health snapshot is invalid")
                cleaned["bond_mode"] = mode
            slaves = row.get("slaves")
            if slaves is not None:
                if (
                    not isinstance(slaves, list)
                    or len(slaves) > MAX_BOND_SLAVES
                    or not all(
                        isinstance(slave, str) and re.fullmatch(r"eth[0-9]{1,4}", slave)
                        for slave in slaves
                    )
                ):
                    raise HealthSnapshotError("NAS health snapshot is invalid")
                cleaned["slaves"] = slaves
        elif "bond_mode" in row or "slaves" in row:
            raise HealthSnapshotError("NAS health snapshot is invalid")
        output.append(cleaned)
    if status == "unavailable" and output:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    return {"data_status": status, "interfaces": output}


def _validate_services(value: object) -> dict:
    if not isinstance(value, Mapping):
        raise HealthSnapshotError("NAS health snapshot is invalid")
    _exact_keys(value, {"data_status", "units"})
    status = _status(value.get("data_status"))
    units = value.get("units")
    if not isinstance(units, list) or len(units) > MAX_SERVICE_ROWS:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    output = []
    for unit in units:
        if not isinstance(unit, Mapping):
            raise HealthSnapshotError("NAS health snapshot is invalid")
        _exact_keys(unit, {"name", "kind", "data_status"}, {"state"})
        name = unit.get("name")
        kind = unit.get("kind")
        unit_status = _status(unit.get("data_status"))
        if kind == "service":
            allowed_names = _SYSTEMD_UNITS
        elif kind == "package":
            allowed_names = _PACKAGE_NAMES
        else:
            raise HealthSnapshotError("NAS health snapshot is invalid")
        if name not in allowed_names:
            raise HealthSnapshotError("NAS health snapshot is invalid")
        cleaned = {"name": name, "kind": kind, "data_status": unit_status}
        state = unit.get("state")
        if unit_status == "ok":
            states = (
                _SERVICE_STATES
                if kind == "service"
                else {
                    "running",
                    "stopped",
                    "non_installed",
                    "broken_by_other",
                }
            )
            if not isinstance(state, str) or state not in states:
                raise HealthSnapshotError("NAS health snapshot is invalid")
            cleaned["state"] = state
        elif state is not None:
            raise HealthSnapshotError("NAS health snapshot is invalid")
        output.append(cleaned)
    if status == "unavailable" and any(unit["data_status"] == "ok" for unit in output):
        raise HealthSnapshotError("NAS health snapshot is invalid")
    return {"data_status": status, "units": output}


def _validate_document(document: object) -> dict:
    if not isinstance(document, Mapping):
        raise HealthSnapshotError("NAS health snapshot is invalid")
    _exact_keys(document, {"schema_version", "captured_at", "power", "ups", "network", "services"})
    if type(document.get("schema_version")) is not int:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    if document["schema_version"] != SCHEMA_VERSION:
        raise HealthSnapshotError("NAS health snapshot has an unsupported schema")
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": _document_timestamp(document.get("captured_at")).isoformat(
            timespec="seconds"
        ),
        "power": _validate_power(document.get("power")),
        "ups": _validate_ups(document.get("ups")),
        "network": _validate_network(document.get("network")),
        "services": _validate_services(document.get("services")),
    }


def read_health_snapshot(path: Path, max_age_seconds: int = 600) -> dict:
    if isinstance(max_age_seconds, bool) or max_age_seconds <= 0:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_HEALTH_SNAPSHOT_BYTES:
                raise HealthSnapshotError("NAS health snapshot is invalid")
            now = time.time()
            if (
                now - details.st_mtime > max_age_seconds
                or details.st_mtime > now + _FUTURE_TOLERANCE_SECONDS
            ):
                raise HealthSnapshotError("NAS health snapshot is stale")
            content = stream.read(MAX_HEALTH_SNAPSHOT_BYTES + 1)
    except OSError:
        raise HealthSnapshotError("NAS health snapshot unavailable") from None
    if len(content) > MAX_HEALTH_SNAPSHOT_BYTES:
        raise HealthSnapshotError("NAS health snapshot is invalid")
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise HealthSnapshotError("NAS health snapshot is invalid") from None
    snapshot = _validate_document(document)
    captured_at = _document_timestamp(snapshot["captured_at"]).timestamp()
    if now - captured_at > max_age_seconds or captured_at > now + _FUTURE_TOLERANCE_SECONDS:
        raise HealthSnapshotError("NAS health snapshot is stale")
    return snapshot
