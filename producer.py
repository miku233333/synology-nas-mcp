"""Produce a small, read-only DSM storage snapshot for the MCP container."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

DSM_COMMAND = (
    "/usr/syno/bin/synowebapi",
    "--exec",
    "api=SYNO.Storage.CGI.Storage",
    "method=load_info",
    "version=1",
)
MAX_DSM_RESPONSE_BYTES = 4 * 1024 * 1024


class ProducerError(Exception):
    """A failure that must not include raw DSM data in logs."""


def _trusted_code(script: Path) -> None:
    parent = script.parent.parent
    directory = script.parent
    module = directory / "status_snapshot.py"
    try:
        parent_info = parent.lstat()
        directory_info = directory.lstat()
        script_info = script.lstat()
        module_info = module.lstat()
    except OSError:
        raise ProducerError("Code files are unavailable") from None
    if (
        not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != 0
        or parent_info.st_mode & 0o022
        or not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != 0
        or stat.S_IMODE(directory_info.st_mode) != 0o700
        or any(
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            for info in (script_info, module_info)
        )
    ):
        raise ProducerError("Code ownership or permissions are unsafe")


def _capture_dsm_data() -> dict:
    try:
        result = subprocess.run(
            DSM_COMMAND,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProducerError("DSM status command failed") from None
    if result.returncode != 0 or len(result.stdout) > MAX_DSM_RESPONSE_BYTES:
        raise ProducerError("DSM status command failed")
    try:
        response = json.loads(result.stdout)
    except (UnicodeDecodeError, ValueError):
        raise ProducerError("DSM status response is invalid") from None
    if (
        not isinstance(response, dict)
        or response.get("success") is not True
        or not isinstance(response.get("data"), dict)
    ):
        raise ProducerError("DSM status response is invalid")
    return response["data"]


def _prepare_output(directory: Path, gid: int) -> None:
    try:
        if not directory.exists():
            directory.mkdir(mode=0o700)
            os.chown(directory, 0, gid)
            directory.chmod(0o750)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != gid
            or stat.S_IMODE(info.st_mode) != 0o750
        ):
            raise ProducerError("Output directory ownership or permissions are unsafe")
        target = directory / "status.json"
        try:
            existing = target.lstat()
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != 0
            or existing.st_gid != gid
            or stat.S_IMODE(existing.st_mode) != 0o640
        ):
            raise ProducerError("Snapshot ownership or permissions are unsafe")
    except OSError:
        raise ProducerError("Output directory is unavailable") from None


def _write_snapshot(directory: Path, gid: int, snapshot: dict, max_bytes: int) -> None:
    encoded = json.dumps(
        snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ProducerError("NAS status snapshot exceeds size limit")
    _prepare_output(directory, gid)
    temporary_path = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(prefix=".status-", dir=directory)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchown(stream.fileno(), 0, gid)
            os.fchmod(stream.fileno(), 0o640)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, directory / "status.json")
        temporary_path = None
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError:
        raise ProducerError("Unable to write NAS status snapshot") from None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def main() -> int:
    if os.geteuid() != 0:
        print("NAS status producer requires root", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description="Write an allowlisted NAS storage snapshot")
    parser.add_argument(
        "--gid", type=int, required=True, help="numeric group ID used by MCP container"
    )
    args = parser.parse_args()
    if args.gid <= 0:
        parser.error("--gid must be a non-root group ID")
    script = Path(__file__).absolute()
    os.umask(0o077)
    try:
        _trusted_code(script)
        sys.path.insert(0, str(script.parent))
        from status_snapshot import MAX_SNAPSHOT_BYTES, snapshot_from_dsm

        snapshot = snapshot_from_dsm(_capture_dsm_data())
        _write_snapshot(script.parent / "output", args.gid, snapshot, MAX_SNAPSHOT_BYTES)
    except (ProducerError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
