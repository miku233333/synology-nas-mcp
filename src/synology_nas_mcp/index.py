"""NAS-local, bounded full-text index for the read-only file tools."""

from __future__ import annotations

import argparse
import errno
import os
import sqlite3
import stat
import sys
import time
from contextlib import closing
from pathlib import Path

from synology_nas_mcp.files import (
    _CONTENT_SEARCH_BINARY_SUFFIXES,
    _MAX_RELATIVE_DEPTH,
    FileStore,
)

_DEFAULT_MAX_ENTRIES = 100_000
_DEFAULT_MAX_TEXT_BYTES = 128 * 1024 * 1024
_DEFAULT_MAX_DB_BYTES = 512 * 1024 * 1024
_DEFAULT_MAX_AGE_SECONDS = 3_600
_MAX_QUERY_CHARS = 200
_SEARCH_TIMEOUT_SECONDS = 5
_DISAPPEARED_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})


class IndexLimitError(ValueError):
    """The configured index budget was reached before the scan completed."""


class SearchIndex:
    """Maintain a private SQLite index outside the mounted NAS data directory."""

    def __init__(
        self,
        db_path: Path,
        files: FileStore,
        *,
        max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_text_bytes: int = _DEFAULT_MAX_TEXT_BYTES,
        max_db_bytes: int = _DEFAULT_MAX_DB_BYTES,
    ) -> None:
        self.db_path = Path(db_path)
        self.files = files
        self.max_age_seconds = max_age_seconds
        self.max_entries = max_entries
        self.max_text_bytes = max_text_bytes
        self.max_db_bytes = max_db_bytes
        if not self.db_path.is_absolute() or ".." in self.db_path.parts:
            raise ValueError("Index path must be absolute and normalized")
        if any(
            value <= 0 for value in (max_age_seconds, max_entries, max_text_bytes, max_db_bytes)
        ):
            raise ValueError("Index limits must be positive")
        if self.db_path.resolve().is_relative_to(self.files._root.resolve()):
            raise ValueError("Index database must be outside the shared data directory")

    def _writer(self) -> sqlite3.Connection:
        parent = self.db_path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_info = parent.lstat()
        if not stat.S_ISDIR(parent_info.st_mode) or parent_info.st_mode & 0o077:
            raise ValueError("Index directory must be private to the service user")
        try:
            self._ensure_outside_share(parent_info)
        except (OSError, ValueError):
            self._mark_existing_partial()
            raise
        try:
            fd = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            database_info = self.db_path.lstat()
            if not stat.S_ISREG(database_info.st_mode) or database_info.st_mode & 0o077:
                raise ValueError("Index database must be a private regular file") from None
        else:
            os.close(fd)
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    path TEXT PRIMARY KEY,
                    path_folded TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    text_bytes INTEGER NOT NULL,
                    truncated INTEGER NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
                    USING fts5(content, tokenize='trigram');
                CREATE TABLE IF NOT EXISTS index_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    state TEXT NOT NULL,
                    last_attempt REAL,
                    last_completed REAL,
                    scanned_entries INTEGER NOT NULL DEFAULT 0,
                    indexed_files INTEGER NOT NULL DEFAULT 0,
                    indexed_text_bytes INTEGER NOT NULL DEFAULT 0,
                    skipped_media INTEGER NOT NULL DEFAULT 0,
                    skipped_oversize INTEGER NOT NULL DEFAULT 0,
                    skipped_special INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    truncated_documents INTEGER NOT NULL DEFAULT 0,
                    updated_documents INTEGER NOT NULL DEFAULT 0,
                    deleted_documents INTEGER NOT NULL DEFAULT 0,
                    source_max_file_bytes INTEGER NOT NULL DEFAULT 0,
                    source_max_text_chars INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO index_state (singleton, state) VALUES (1, 'unavailable');
                """
            )
        except sqlite3.Error:
            connection.close()
            raise ValueError("SQLite FTS5 trigram support is required") from None
        return connection

    def _mark_existing_partial(self) -> None:
        """Invalidate an older index when a preflight check blocks refresh."""
        try:
            database_info = self.db_path.lstat()
            if not stat.S_ISREG(database_info.st_mode) or database_info.st_mode & 0o077:
                return
            connection = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=5)
            try:
                connection.execute(
                    "UPDATE index_state SET state = 'partial', last_attempt = ? "
                    "WHERE singleton = 1",
                    (time.time(),),
                )
                connection.commit()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            pass

    def _ensure_outside_share(self, index_directory: os.stat_result) -> None:
        """Reject bind-mount aliases of directories within the readable share."""
        target = (index_directory.st_dev, index_directory.st_ino)
        pending: list[tuple[str, ...]] = [()]
        scanned_entries = 0
        while pending:
            parts = pending.pop()
            try:
                directory_fd = self.files._open_directory(parts)
            except OSError as error:
                if parts and error.errno in _DISAPPEARED_ERRNOS:
                    continue
                raise
            try:
                current = os.fstat(directory_fd)
                if (current.st_dev, current.st_ino) == target:
                    raise ValueError("Index directory must be outside the shared data directory")
                with os.scandir(directory_fd) as iterator:
                    for entry in iterator:
                        scanned_entries += 1
                        if scanned_entries > self.max_entries:
                            raise IndexLimitError("Index path check exceeds the entry limit")
                        try:
                            metadata = entry.stat(follow_symlinks=False)
                        except OSError as error:
                            if error.errno in _DISAPPEARED_ERRNOS:
                                continue
                            raise
                        if stat.S_ISDIR(metadata.st_mode):
                            child_parts = parts + (entry.name,)
                            if len(child_parts) > _MAX_RELATIVE_DEPTH:
                                raise IndexLimitError("Index path check exceeds the depth limit")
                            if (metadata.st_dev, metadata.st_ino) == target:
                                raise ValueError(
                                    "Index directory must be outside the shared data directory"
                                )
                            pending.append(child_parts)
            finally:
                os.close(directory_fd)

    def _reader(self) -> sqlite3.Connection:
        if not self.db_path.is_file() or self.db_path.is_symlink():
            raise ValueError("Search index is unavailable")
        connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _empty_stats() -> dict[str, int]:
        return {
            "scanned_entries": 0,
            "indexed_files": 0,
            "indexed_text_bytes": 0,
            "skipped_media": 0,
            "skipped_oversize": 0,
            "skipped_special": 0,
            "errors": 0,
            "truncated_documents": 0,
            "updated_documents": 0,
            "deleted_documents": 0,
        }

    def rebuild_or_refresh(self) -> dict:
        """Atomically refresh changed documents and remove files no longer present."""
        connection = self._writer()
        attempt = time.time()
        stats = self._empty_stats()
        try:
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            max_pages = self.max_db_bytes // page_size
            current_pages = connection.execute("PRAGMA page_count").fetchone()[0]
            if max_pages < 1 or current_pages > max_pages:
                raise IndexLimitError("Index database exceeds the size limit")
            applied_pages = connection.execute(f"PRAGMA max_page_count = {max_pages}").fetchone()[0]
            if applied_pages > max_pages:
                raise IndexLimitError("Index database exceeds the size limit")
            connection.execute("BEGIN IMMEDIATE")
            previous = {
                row["path"]: row
                for row in connection.execute(
                    "SELECT rowid, path, size_bytes, mtime_ns, device, inode, "
                    "text_bytes, truncated FROM documents"
                )
            }
            if len(previous) > self.max_entries:
                raise IndexLimitError("Existing index exceeds the entry limit")
            previous_state = connection.execute(
                "SELECT source_max_file_bytes, source_max_text_chars FROM index_state "
                "WHERE singleton = 1"
            ).fetchone()
            force_reextract = previous_state is None or (
                previous_state["source_max_file_bytes"] != self.files.max_file_bytes
                or previous_state["source_max_text_chars"] != self.files.max_text_chars
            )
            seen: set[str] = set()
            pending: list[tuple[str, ...]] = [()]
            while pending:
                parts = pending.pop()
                try:
                    directory_fd = self.files._open_directory(parts)
                except OSError as error:
                    if parts and error.errno in _DISAPPEARED_ERRNOS:
                        stats["errors"] += 1
                        continue
                    raise
                try:
                    with os.scandir(directory_fd) as iterator:
                        for entry in iterator:
                            stats["scanned_entries"] += 1
                            if stats["scanned_entries"] > self.max_entries:
                                raise IndexLimitError("Directory scan exceeds the entry limit")
                            try:
                                metadata = entry.stat(follow_symlinks=False)
                            except OSError as error:
                                if error.errno in _DISAPPEARED_ERRNOS:
                                    stats["errors"] += 1
                                    continue
                                raise
                            child_parts = parts + (entry.name,)
                            if len(child_parts) > _MAX_RELATIVE_DEPTH:
                                raise IndexLimitError(
                                    "Directory depth exceeds the file access limit"
                                )
                            if stat.S_ISDIR(metadata.st_mode):
                                pending.append(child_parts)
                                continue
                            if not stat.S_ISREG(metadata.st_mode):
                                stats["skipped_special"] += 1
                                continue
                            if (
                                Path(entry.name).suffix.casefold()
                                in _CONTENT_SEARCH_BINARY_SUFFIXES
                            ):
                                stats["skipped_media"] += 1
                                continue
                            if metadata.st_size > self.files.max_file_bytes:
                                stats["skipped_oversize"] += 1
                                continue

                            path = self.files._display_path(child_parts)
                            old = previous.get(path)
                            same_file = (
                                old is not None
                                and not force_reextract
                                and (
                                    old["size_bytes"] == metadata.st_size
                                    and old["mtime_ns"] == metadata.st_mtime_ns
                                    and old["device"] == metadata.st_dev
                                    and old["inode"] == metadata.st_ino
                                )
                            )
                            if same_file:
                                seen.add(path)
                                stats["indexed_files"] += 1
                                stats["indexed_text_bytes"] += old["text_bytes"]
                                stats["truncated_documents"] += old["truncated"]
                                if stats["indexed_text_bytes"] > self.max_text_bytes:
                                    raise IndexLimitError("Indexed text exceeds the size limit")
                                continue

                            try:
                                document = self.files.read_file(path)
                                file_fd, _ = self.files._open_regular_file(child_parts)
                                try:
                                    after = os.fstat(file_fd)
                                finally:
                                    os.close(file_fd)
                                if (
                                    document["size"] != metadata.st_size
                                    or after.st_size != metadata.st_size
                                    or after.st_mtime_ns != metadata.st_mtime_ns
                                    or after.st_dev != metadata.st_dev
                                    or after.st_ino != metadata.st_ino
                                ):
                                    raise ValueError("File changed during indexing")
                            except (OSError, ValueError):
                                stats["errors"] += 1
                                continue

                            text_bytes = len(document["content"].encode("utf-8"))
                            stats["indexed_text_bytes"] += text_bytes
                            if stats["indexed_text_bytes"] > self.max_text_bytes:
                                raise IndexLimitError("Indexed text exceeds the size limit")
                            self._upsert_document(
                                connection,
                                path,
                                metadata,
                                document["content"],
                                text_bytes,
                                document["truncated"],
                                old,
                            )
                            seen.add(path)
                            stats["indexed_files"] += 1
                            stats["updated_documents"] += 1
                            stats["truncated_documents"] += int(document["truncated"])
                finally:
                    os.close(directory_fd)

            for path, old in previous.items():
                if path not in seen:
                    connection.execute("DELETE FROM documents_fts WHERE rowid = ?", (old["rowid"],))
                    connection.execute("DELETE FROM documents WHERE path = ?", (path,))
                    stats["deleted_documents"] += 1

            page_count = connection.execute("PRAGMA page_count").fetchone()[0]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            if page_count * page_size > self.max_db_bytes:
                raise IndexLimitError("Index database exceeds the size limit")
            completed = time.time()
            connection.execute(
                """UPDATE index_state SET state = 'complete', last_attempt = ?,
                   last_completed = ?, scanned_entries = ?, indexed_files = ?,
                   indexed_text_bytes = ?, skipped_media = ?, skipped_oversize = ?,
                   skipped_special = ?, errors = ?, truncated_documents = ?,
                   updated_documents = ?, deleted_documents = ?,
                   source_max_file_bytes = ?, source_max_text_chars = ?
                   WHERE singleton = 1""",
                (
                    attempt,
                    completed,
                    *(stats[key] for key in stats),
                    self.files.max_file_bytes,
                    self.files.max_text_chars,
                ),
            )
            connection.commit()
            return self._status_from_connection(connection)
        except Exception as error:
            connection.rollback()
            try:
                connection.execute(
                    """UPDATE index_state SET state = 'partial', last_attempt = ?,
                       scanned_entries = ?, skipped_media = ?, skipped_oversize = ?,
                       skipped_special = ?, errors = ?, truncated_documents = ?
                       WHERE singleton = 1""",
                    (
                        attempt,
                        stats["scanned_entries"],
                        stats["skipped_media"],
                        stats["skipped_oversize"],
                        stats["skipped_special"],
                        stats["errors"],
                        stats["truncated_documents"],
                    ),
                )
                connection.commit()
            except sqlite3.Error:
                connection.rollback()
            if (
                isinstance(error, sqlite3.Error)
                and getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
            ):
                raise IndexLimitError("Index database reached its size limit") from None
            raise
        finally:
            connection.close()

    @staticmethod
    def _upsert_document(
        connection: sqlite3.Connection,
        path: str,
        metadata: os.stat_result,
        text: str,
        text_bytes: int,
        truncated: bool,
        old: sqlite3.Row | None,
    ) -> None:
        if old is not None:
            connection.execute("DELETE FROM documents_fts WHERE rowid = ?", (old["rowid"],))
        connection.execute(
            """INSERT INTO documents (
                path, path_folded, size_bytes, mtime_ns, device, inode,
                text, text_bytes, truncated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                path_folded = excluded.path_folded,
                size_bytes = excluded.size_bytes,
                mtime_ns = excluded.mtime_ns,
                device = excluded.device,
                inode = excluded.inode,
                text = excluded.text,
                text_bytes = excluded.text_bytes,
                truncated = excluded.truncated""",
            (
                path,
                path.casefold(),
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_dev,
                metadata.st_ino,
                text,
                text_bytes,
                int(truncated),
            ),
        )
        rowid = connection.execute(
            "SELECT rowid FROM documents WHERE path = ?", (path,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO documents_fts (rowid, content) VALUES (?, ?)",
            (rowid, text.casefold()),
        )

    def _status_from_connection(self, connection: sqlite3.Connection) -> dict:
        row = connection.execute("SELECT * FROM index_state WHERE singleton = 1").fetchone()
        if row is None:
            return {"state": "unavailable", "fresh": False}
        result = dict(row)
        result.pop("singleton", None)
        completed = result["last_completed"]
        age = time.time() - completed if completed is not None else None
        result["age_seconds"] = age
        result["fresh"] = (
            result["state"] == "complete" and age is not None and 0 <= age <= self.max_age_seconds
        )
        if result["state"] == "complete" and not result["fresh"]:
            result["state"] = "stale"
        result["stats"] = {key: result[key] for key in self._empty_stats()}
        return result

    def status(self) -> dict:
        """Report index freshness and aggregate exclusions without listing private paths."""
        try:
            self._require_data_root()
            with closing(self._reader()) as connection:
                return self._status_from_connection(connection)
        except (OSError, sqlite3.Error, ValueError):
            return {"state": "unavailable", "fresh": False}

    def _require_data_root(self) -> None:
        try:
            root_fd = self.files._open_root()
        except OSError:
            raise ValueError("Shared data directory is unavailable") from None
        os.close(root_fd)

    def _is_current(self, path: str, row: sqlite3.Row) -> bool:
        try:
            parts = self.files._path_parts(path)
            file_fd, _ = self.files._open_regular_file(parts)
            try:
                metadata = os.fstat(file_fd)
            finally:
                os.close(file_fd)
        except (OSError, ValueError):
            return False
        return (
            metadata.st_size == row["size_bytes"]
            and metadata.st_mtime_ns == row["mtime_ns"]
            and metadata.st_dev == row["device"]
            and metadata.st_ino == row["inode"]
        )

    @staticmethod
    def _snippet(text: str, needle: str) -> str | None:
        position = text.casefold().find(needle)
        if position < 0:
            return None
        folded_offset = 0
        for original_offset, character in enumerate(text):
            folded_offset += len(character.casefold())
            if folded_offset > position:
                break
        start = max(0, original_offset - 80)
        return text[start : start + 240]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search filenames and indexed text, excluding changed or removed live files."""
        if not isinstance(query, str) or not query.strip() or "\x00" in query:
            raise ValueError("Search query must not be empty")
        if len(query) > _MAX_QUERY_CHARS:
            raise ValueError("Search query is too long")
        self.files._validate_limit(limit)
        folded = query.casefold()
        escaped = folded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"
        self._require_data_root()
        try:
            with closing(self._reader()) as connection:
                state = self._status_from_connection(connection)
                if not state["fresh"]:
                    raise ValueError("Search index is unavailable, partial or stale")
                deadline = time.monotonic() + _SEARCH_TIMEOUT_SECONDS
                connection.set_progress_handler(lambda: time.monotonic() > deadline, 1_000)
                results: list[dict] = []
                seen: set[str] = set()
                metadata_columns = "d.path, d.size_bytes, d.mtime_ns, d.device, d.inode"
                if len(folded) >= 3:
                    phrase = '"' + folded.replace('"', '""') + '"'
                    contents = connection.execute(
                        f"SELECT {metadata_columns}, d.text FROM documents_fts f "
                        "JOIN documents d ON d.rowid = f.rowid "
                        "WHERE documents_fts MATCH ? ORDER BY d.path",
                        (phrase,),
                    )
                else:
                    contents = connection.execute(
                        f"SELECT {metadata_columns}, d.text FROM documents_fts f "
                        "JOIN documents d ON d.rowid = f.rowid "
                        "WHERE f.content LIKE ? ESCAPE '\\' ORDER BY d.path",
                        (like_pattern,),
                    )
                for row in contents:
                    path = row["path"]
                    if not self._is_current(path, row):
                        continue
                    snippet = self._snippet(row["text"], folded)
                    if snippet is None:
                        continue
                    seen.add(path)
                    results.append({"path": path, "snippet": snippet})
                    if len(results) >= limit:
                        return results
                paths = connection.execute(
                    f"SELECT {metadata_columns} FROM documents d "
                    "WHERE d.path_folded LIKE ? ESCAPE '\\' ORDER BY d.path",
                    (like_pattern,),
                )
                for row in paths:
                    path = row["path"]
                    if path in seen or not self._is_current(path, row):
                        continue
                    results.append({"path": path})
                    if len(results) >= limit:
                        return results
                if not results and state["errors"]:
                    raise ValueError("No match in indexed files; some files could not be indexed")
                if not results:
                    self._require_data_root()
                return results
        except sqlite3.Error:
            raise ValueError(
                "Search index is unavailable or the query exceeded its time limit"
            ) from None


def _env_integer(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh the NAS-local text search index")
    parser.add_argument("--loop", action="store_true", help="refresh periodically")
    parser.add_argument("--interval-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    path = os.environ.get("NAS_INDEX_PATH", "")
    if not path:
        parser.error("NAS_INDEX_PATH must be set")
    try:
        files = FileStore(
            Path(os.environ.get("NAS_DATA_ROOT", "/data")),
            max_file_bytes=_env_integer("NAS_MAX_FILE_BYTES", 2_097_152),
            max_text_chars=_env_integer("NAS_MAX_TEXT_CHARS", 50_000),
        )
        index = SearchIndex(
            Path(path),
            files,
            max_age_seconds=_env_integer("NAS_INDEX_MAX_AGE_SECONDS", 3_600),
            max_entries=_env_integer("NAS_INDEX_MAX_ENTRIES", 100_000),
            max_text_bytes=_env_integer("NAS_INDEX_MAX_TEXT_BYTES", 128 * 1024 * 1024),
            max_db_bytes=_env_integer("NAS_INDEX_MAX_DB_BYTES", 512 * 1024 * 1024),
        )
    except (OSError, ValueError):
        print("Index configuration is invalid.", file=sys.stderr)
        raise SystemExit(1) from None

    while True:
        try:
            result = index.rebuild_or_refresh()
            print(
                "Index refreshed: "
                f"documents={result['indexed_files']} "
                f"skipped={result['skipped_media'] + result['skipped_oversize']} "
                f"errors={result['errors']}",
                flush=True,
            )
        except Exception as error:
            print(f"Index refresh failed: {type(error).__name__}", file=sys.stderr, flush=True)
            if not args.loop:
                raise SystemExit(1) from None
        if not args.loop:
            break
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
