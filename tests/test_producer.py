import importlib.util
import json
import subprocess
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
