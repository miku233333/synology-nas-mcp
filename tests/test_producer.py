import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("producer", Path(__file__).parents[1] / "producer.py")
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


def test_capture_uses_fixed_local_command_and_parses_success(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"success": True, "data": {}}).encode()
        )

    monkeypatch.setattr(producer.subprocess, "run", run)
    assert producer._capture_dsm_data() == {}
    assert calls[0][0] == (
        "/usr/syno/bin/synowebapi",
        "--exec",
        "api=SYNO.Storage.CGI.Storage",
        "method=load_info",
        "version=1",
    )
    assert calls[0][1]["timeout"] == 20


@pytest.mark.parametrize(
    "result",
    [
        SimpleNamespace(returncode=1, stdout=b"private response"),
        SimpleNamespace(returncode=0, stdout=b"not json"),
        SimpleNamespace(returncode=0, stdout=b'{"success":false,"data":{}}'),
    ],
)
def test_capture_errors_do_not_expose_raw_response(monkeypatch, result):
    monkeypatch.setattr(producer.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(producer.ProducerError) as error:
        producer._capture_dsm_data()
    assert "private response" not in str(error.value)


def test_capture_timeout_is_safe(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("secret command", 20, output=b"private response")

    monkeypatch.setattr(producer.subprocess, "run", timeout)
    with pytest.raises(producer.ProducerError) as error:
        producer._capture_dsm_data()
    assert "secret" not in str(error.value)


def test_health_write_failure_does_not_replace_the_storage_snapshot(tmp_path, monkeypatch):
    def prepare(directory, gid, filename):
        if filename == "health.json":
            raise producer.ProducerError("Health output is unavailable")

    monkeypatch.setattr(producer, "_prepare_output", prepare)
    monkeypatch.setattr(producer.os, "fchown", lambda *args: None)
    monkeypatch.setattr(producer.os, "fchmod", lambda *args: None)
    producer._write_snapshot(tmp_path, 1, {"storage": "current"}, 1024, "status.json")

    with pytest.raises(producer.ProducerError):
        producer._write_snapshot(tmp_path, 1, {"health": "current"}, 1024, "health.json")

    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8")) == {
        "storage": "current"
    }


def test_storage_failure_does_not_block_health_snapshot(monkeypatch, tmp_path):
    writes = []

    def capture():
        raise producer.ProducerError("DSM status command failed")

    monkeypatch.setattr(producer, "_capture_dsm_data", capture)
    monkeypatch.setattr(
        producer,
        "_write_snapshot",
        lambda directory, gid, snapshot, max_bytes, filename: writes.append((filename, snapshot)),
    )

    failed = producer._produce_snapshots(
        tmp_path,
        1,
        1024,
        lambda data: data,
        1024,
        lambda: {"health": "current"},
    )

    assert failed is True
    assert writes == [("health.json", {"health": "current"})]


def test_health_failure_does_not_block_storage_snapshot(monkeypatch, tmp_path):
    writes = []
    monkeypatch.setattr(producer, "_capture_dsm_data", lambda: {"storage": "source"})
    monkeypatch.setattr(
        producer,
        "_write_snapshot",
        lambda directory, gid, snapshot, max_bytes, filename: writes.append((filename, snapshot)),
    )

    def collect_health():
        raise ValueError("unavailable")

    failed = producer._produce_snapshots(
        tmp_path,
        1,
        1024,
        lambda data: {"storage": "current"},
        1024,
        collect_health,
    )

    assert failed is True
    assert writes == [("status.json", {"storage": "current"})]


def test_main_reports_a_fixed_error_after_an_isolated_failure(monkeypatch, capsys):
    status_snapshot = types.ModuleType("status_snapshot")
    status_snapshot.MAX_SNAPSHOT_BYTES = 1024
    status_snapshot.snapshot_from_dsm = lambda data: data
    health_snapshot = types.ModuleType("health_snapshot")
    health_snapshot.MAX_HEALTH_SNAPSHOT_BYTES = 1024
    health_snapshot.collect_health = lambda: {"health": "current"}
    monkeypatch.setitem(sys.modules, "status_snapshot", status_snapshot)
    monkeypatch.setitem(sys.modules, "health_snapshot", health_snapshot)
    monkeypatch.setattr(producer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(producer, "_trusted_code", lambda script: None)
    monkeypatch.setattr(producer, "_produce_snapshots", lambda *args: True)
    monkeypatch.setattr(producer.sys, "argv", ["producer.py", "--gid", "100"])

    assert producer.main() == 1
    assert capsys.readouterr().err == "NAS snapshot update failed\n"
