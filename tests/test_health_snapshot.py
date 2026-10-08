import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from synology_nas_mcp import health_snapshot


def health_document(captured_at=None):
    return {
        "schema_version": 1,
        "captured_at": (captured_at or datetime.now(UTC)).isoformat(timespec="seconds"),
        "power": {
            "data_status": "ok",
            "running": True,
            "uptime_seconds": 123,
            "psu_health": "unknown",
        },
        "ups": {
            "data_status": "ok",
            "status_flags": ["ALARM", "OL", "CHRG"],
            "battery_charge_percent": 73,
            "battery_runtime_seconds": 1800,
        },
        "network": {
            "data_status": "ok",
            "interfaces": [
                {
                    "name": "bond9",
                    "kind": "bond",
                    "link_state": "up",
                    "speed_mbps": 2000,
                    "bond_mode": "802_3ad",
                    "slaves": ["eth4", "eth5"],
                }
            ],
        },
        "services": {"data_status": "unavailable", "units": []},
    }


def write_snapshot(path, document=None):
    path.write_text(json.dumps(document or health_document()), encoding="utf-8")


def test_ups_preserves_alarm_flags_without_declaring_ol_healthy(monkeypatch):
    monkeypatch.setattr(health_snapshot, "_candidate", lambda paths: Path("/bin/upsc"))
    monkeypatch.setattr(
        health_snapshot,
        "_run",
        lambda argv: (
            b"ups.status: ALARM OL CHRG UNKNOWN\nbattery.charge: 73\nbattery.runtime: 1800\n"
        ),
    )

    assert health_snapshot._ups() == {
        "data_status": "ok",
        "status_flags": ["ALARM", "OL", "CHRG"],
        "battery_charge_percent": 73,
        "battery_runtime_seconds": 1800,
    }


def test_ups_without_an_allowlisted_status_is_unavailable(monkeypatch):
    monkeypatch.setattr(health_snapshot, "_candidate", lambda paths: Path("/bin/upsc"))
    monkeypatch.setattr(health_snapshot, "_run", lambda argv: b"ups.status: NEWFLAG\n")

    assert health_snapshot._ups() == {"data_status": "unavailable"}


def test_network_only_returns_allowlisted_interfaces_and_bond_fields(tmp_path, monkeypatch):
    sys_class_net = tmp_path / "net"
    for name, state, speed in (
        ("eth4", "up", "1000"),
        ("bond9", "up", "2000"),
        ("lo", "unknown", "0"),
    ):
        directory = sys_class_net / name
        directory.mkdir(parents=True)
        (directory / "operstate").write_text(state, encoding="ascii")
        (directory / "speed").write_text(speed, encoding="ascii")
    bonding = tmp_path / "bonding"
    bonding.mkdir()
    (bonding / "bond9").write_text(
        "Bonding Mode: IEEE 802.3ad Dynamic link aggregation\n"
        "Slave Interface: eth4\n"
        "Slave Interface: eth5\n",
        encoding="ascii",
    )
    monkeypatch.setattr(health_snapshot, "_SYS_CLASS_NET", sys_class_net)
    monkeypatch.setattr(health_snapshot, "_PROC_BONDING", bonding)

    assert health_snapshot._network() == {
        "data_status": "ok",
        "interfaces": [
            {
                "name": "bond9",
                "kind": "bond",
                "link_state": "up",
                "speed_mbps": 2000,
                "bond_mode": "802_3ad",
                "slaves": ["eth4", "eth5"],
            },
            {"name": "eth4", "kind": "physical", "link_state": "up", "speed_mbps": 1000},
        ],
    }


def test_network_does_not_assign_an_unconfirmed_bond_mode(tmp_path, monkeypatch):
    sys_class_net = tmp_path / "net"
    directory = sys_class_net / "bond9"
    directory.mkdir(parents=True)
    (directory / "operstate").write_text("up", encoding="ascii")
    (directory / "speed").write_text("2000", encoding="ascii")
    bonding = tmp_path / "bonding"
    bonding.mkdir()
    (bonding / "bond9").write_text(
        "Bonding Mode: load balancing (round-robin) (balance-rr)\n",
        encoding="ascii",
    )
    monkeypatch.setattr(health_snapshot, "_SYS_CLASS_NET", sys_class_net)
    monkeypatch.setattr(health_snapshot, "_PROC_BONDING", bonding)

    assert health_snapshot._network()["interfaces"] == [
        {"name": "bond9", "kind": "bond", "link_state": "up", "speed_mbps": 2000}
    ]


def test_service_and_package_commands_are_fixed_and_require_confirmed_states(monkeypatch):
    calls = []

    def run(argv, accepted_returncodes=(0,)):
        calls.append((argv, accepted_returncodes))
        if argv[1] == "is-active":
            return b"active\n"
        return b'{"status":"running"}'

    monkeypatch.setattr(health_snapshot, "_run", run)
    assert health_snapshot._service_details(Path("/bin/systemctl"), "sshd.service") == {
        "name": "sshd.service",
        "kind": "service",
        "data_status": "ok",
        "state": "active",
    }
    assert health_snapshot._package_details(Path("/usr/syno/bin/synopkg"), "SMBService") == {
        "name": "SMBService",
        "kind": "package",
        "data_status": "ok",
        "state": "running",
    }
    assert calls == [
        (("/bin/systemctl", "is-active", "sshd.service"), (0, 3)),
        (("/usr/syno/bin/synopkg", "status", "SMBService"), (0,)),
    ]


def test_reader_rejects_stale_oversized_symlink_and_unknown_fields(tmp_path):
    path = tmp_path / "health.json"
    write_snapshot(path, health_document(datetime.now(UTC) - timedelta(minutes=11)))
    with pytest.raises(health_snapshot.HealthSnapshotError, match="stale"):
        health_snapshot.read_health_snapshot(path)

    path.write_bytes(b"x" * (health_snapshot.MAX_HEALTH_SNAPSHOT_BYTES + 1))
    with pytest.raises(health_snapshot.HealthSnapshotError, match="invalid"):
        health_snapshot.read_health_snapshot(path)

    write_snapshot(path)
    link = tmp_path / "health-link.json"
    link.symlink_to(path)
    with pytest.raises(health_snapshot.HealthSnapshotError, match="unavailable"):
        health_snapshot.read_health_snapshot(link)

    document = health_document()
    document["secret"] = "not allowlisted"
    write_snapshot(path, document)
    with pytest.raises(health_snapshot.HealthSnapshotError, match="invalid"):
        health_snapshot.read_health_snapshot(path)


def test_reader_handles_malicious_list_and_dict_values_as_validation_errors(tmp_path):
    path = tmp_path / "health.json"
    document = health_document()
    document["power"]["data_status"] = []
    write_snapshot(path, document)
    with pytest.raises(health_snapshot.HealthSnapshotError, match="invalid"):
        health_snapshot.read_health_snapshot(path)

    document = health_document()
    document["network"]["interfaces"][0]["kind"] = {"unexpected": "object"}
    write_snapshot(path, document)
    with pytest.raises(health_snapshot.HealthSnapshotError, match="invalid"):
        health_snapshot.read_health_snapshot(path)


def test_reader_rejects_stale_mtime_and_uses_strict_ups_allowlist(tmp_path):
    path = tmp_path / "health.json"
    document = health_document()
    document["ups"]["status_flags"] = ["OL", "PRIVATE"]
    write_snapshot(path, document)
    with pytest.raises(health_snapshot.HealthSnapshotError, match="invalid"):
        health_snapshot.read_health_snapshot(path)

    write_snapshot(path)
    old = time.time() - 660
    os.utime(path, (old, old))
    with pytest.raises(health_snapshot.HealthSnapshotError, match="stale"):
        health_snapshot.read_health_snapshot(path)
