import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from synology_nas_mcp.status_snapshot import (
    MAX_SNAPSHOT_BYTES,
    StatusSnapshotError,
    read_status_snapshot,
    snapshot_from_dsm,
)


def dsm_storage():
    return {
        "storagePools": [
            {
                "id": "pool1",
                "status": "attention",
                "summary_status": "attention",
                "raidType": "shr",
                "size": {"total": "12000000000000", "used": "9000000000000"},
                "cacheStatus": "attention",
                "suggestions": [
                    {
                        "str": "volume_usage_suggestion",
                        "type": "warning",
                        "section": "volume",
                        "arg": {"private_path": "/volume1/private"},
                    }
                ],
                "notes": [{"secret": "not for MCP"}],
            }
        ],
        "volumes": [
            {
                "id": "volume1",
                "status": "attention",
                "fs_type": "btrfs",
                "size": {"total": "10000000000000", "used": "8000000000000"},
            }
        ],
        "disks": [
            {
                "id": "disk1",
                "model": "Example HDD",
                "status": "normal",
                "overview_status": "normal",
                "smart_status": "normal",
                "temp": 34,
                "size_total": "8000000000000",
                "remain_life": {"trustable": True, "value": 89},
                "serial": "SERIAL-DO-NOT-RETURN",
                "mac": "00:11:22:33:44:55",
            },
            {
                "id": "disk2",
                "smart_status": "normal",
                "remain_life": {"trustable": False, "value": -1},
            },
        ],
        "ssdCaches": [
            {
                "id": "cache1",
                "status": "has_migrated_disk",
                "summary_status": "attention",
                "mode": "read",
                "size": {"total": "1000000000000", "occupied": "200000000000", "reusable": "0"},
                "hit_rate": 86,
                "hit_rate_write": 0,
                "mountSpaceId": "/volume1/private",
            }
        ],
    }


def test_snapshot_allowlist_preserves_status_and_capacity_without_secrets():
    snapshot = snapshot_from_dsm(dsm_storage(), datetime(2026, 9, 26, tzinfo=UTC))
    assert snapshot["pools"][0]["status"] == "attention"
    assert snapshot["pools"][0]["total_size_value"] == 12000000000000
    assert "used_size_value" not in snapshot["pools"][0]
    assert snapshot["pools"][0]["suggestions"] == [
        {"str": "volume_usage_suggestion", "type": "warning", "section": "volume"}
    ]
    assert snapshot["volumes"][0]["used_size_value"] == 8000000000000
    assert snapshot["disks"][0]["smart_status"] == "normal"
    assert snapshot["disks"][0]["remaining_life_value"] == 89
    assert "remaining_life_value" not in snapshot["disks"][1]
    assert snapshot["ssd_caches"][0]["mode"] == "read"
    assert snapshot["ssd_caches"][0]["total_size_value"] == 1000000000000
    assert snapshot["ssd_cache_data_status"] == "reported"
    serialized = json.dumps(snapshot)
    for secret in ("SERIAL-DO-NOT-RETURN", "00:11:22", "/volume1/private", "not for MCP"):
        assert secret not in serialized


def write_snapshot(path, *, captured_at=None):
    snapshot = snapshot_from_dsm(dsm_storage(), captured_at)
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return snapshot


def test_reader_rejects_stale_capture_and_stale_file(tmp_path):
    path = tmp_path / "status.json"
    write_snapshot(path, captured_at=datetime.now(UTC) - timedelta(minutes=11))
    with pytest.raises(StatusSnapshotError, match="stale"):
        read_status_snapshot(path)
    write_snapshot(path)
    old = time.time() - 660
    os.utime(path, (old, old))
    with pytest.raises(StatusSnapshotError, match="stale"):
        read_status_snapshot(path)


def test_reader_rejects_oversized_or_invalid_snapshot(tmp_path):
    path = tmp_path / "status.json"
    path.write_bytes(b"x" * (MAX_SNAPSHOT_BYTES + 1))
    with pytest.raises(StatusSnapshotError, match="invalid"):
        read_status_snapshot(path)
    snapshot = write_snapshot(path)
    snapshot["schema_version"] = 999
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    with pytest.raises(StatusSnapshotError, match="unsupported schema"):
        read_status_snapshot(path)


def test_reader_revalidates_allowlist_and_rejects_symlink(tmp_path):
    path = tmp_path / "status.json"
    snapshot = write_snapshot(path)
    snapshot["disks"][0]["serial"] = "SERIAL-DO-NOT-RETURN"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    assert "serial" not in read_status_snapshot(path)["disks"][0]
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(StatusSnapshotError, match="unavailable"):
        read_status_snapshot(link)


def test_empty_or_missing_cache_does_not_claim_no_cache():
    data = dsm_storage()
    data["ssdCaches"] = []
    assert snapshot_from_dsm(data)["ssd_cache_data_status"] == "empty_or_unavailable"
    del data["ssdCaches"]
    assert snapshot_from_dsm(data)["ssd_cache_data_status"] == "unavailable"


def test_suggestion_only_accepts_fixed_style_tokens():
    data = dsm_storage()
    data["storagePools"][0]["suggestions"] = [
        {"str": "disk serial ABC123", "type": "warning", "section": "volume"},
        {"str": "volume_usage_suggestion", "type": "warning", "section": "volume"},
    ]
    assert snapshot_from_dsm(data)["pools"][0]["suggestions"] == [
        {"str": "volume_usage_suggestion", "type": "warning", "section": "volume"},
    ]
